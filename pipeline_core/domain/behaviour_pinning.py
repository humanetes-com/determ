"""Which harvested behaviours a generated test block PINS — pure domain.

Coverage augmentation admits a block when its tests pass on the implementation
still on disk (`oracle_admission.judge_block`). Passing is a low bar for what a
test ASSERTS: on the 2026-08 anyhow trees, `test_error_impl_debug_fmt` checks
`format!("{:?}", err_impl).contains("impl debug test")` and is admitted, while
the crate's own `tests/test_fmt.rs` pins the WHOLE `{:#?}` rendering
(`EXPECTED_ALTDEBUG_F`, a 4-line literal). The harvested behaviour suite
(`.skeleton/_behaviours_<cluster>.json`) carries exactly those observables, and
until this module nothing in t0c read it — so the generated oracle froze
fragments and internals instead of the observables a reconstruction is judged on.

A behaviour is PINNED by a block when one of its `#[test]` items asserts the
behaviour's exact observable:

- a string-kind outcome (Display / Debug / alternate Debug / rendered return /
  normalized stdout / panic message) is pinned when the block asserts the whole
  `then.value` as an exact string literal — the literal is a top-level argument
  of `assert_eq!`/`assert_ne!` (or an `assert`-prefixed helper), or an operand of
  a top-level `==`/`!=` inside any `assert*` invocation; a panic message is also
  pinned by `#[should_panic(expected = "…")]`. Literals are Rust-unescaped, and a
  `const`/`static`/`let` binding to a literal resolves to it, so
  `assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …))` pins. Values shorter
  than 8 characters are ignored — "3" or "ok" would pin by coincidence.
  `contains(…)` never pins, whatever it contains: the contract is exact equality.
- a type-identity outcome is pinned when an `assert*` carries `is::<T>` /
  `downcast::<T>` / `downcast_ref::<T>` / `downcast_mut::<T>` naming the same
  `T` (whitespace-normalized, `std::`/`core::` prefix ignored) with the same
  polarity: `!…`, `.is_err()`, `.is_none()`, `.unwrap_err()`, `== false`,
  `assert_ne!`, or a `None`/`Err(` pattern after the call read as `ok=False`.

Kinds are compared by name so the module runs against the behaviour vocabulary
whether or not `ALT_DEBUG_STRING` / `TYPE_IDENTITY` / `BehaviourSpec.klass`
have landed in `behaviour.py`. Everything here is a token scan — no regex.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from .behaviour import BehaviourSpec, example_code
from .rust_scan import fn_name_on_line, is_test_attr_line

_STRING_KINDS = frozenset({
    "DISPLAY_STRING", "DEBUG_STRING", "ALT_DEBUG_STRING",
    "RETURN_RENDERED", "NORMALIZED_STDOUT", "PANIC_MESSAGE",
})
_IDENTITY_KIND = "TYPE_IDENTITY"
MIN_PINNABLE_VALUE_CHARS = 8

_IDENTITY_METHODS = ("is", "downcast", "downcast_ref", "downcast_mut")
_NEGATING_TAILS = (".is_err()", ".is_none()", ".unwrap_err()")
_TRIVIAL_ARGS = frozenset({"true", "false", "()"})
_WEAK_PREDICATES = (".is_ok()", ".is_err()", ".is_some()", ".is_none()",
                    ".contains(", ".is_empty()", ".unwrap()", ".expect(")
_STD_PREFIXES = ("std::", "core::", "alloc::")


# --- lexing ------------------------------------------------------------------

def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _read_string_literal(text: str, at: int) -> tuple[str, int] | None:
    """The value of the string literal opening at `at` and the index past its
    close. Handles `"…"`, `b"…"`, `r"…"`, `r#"…"#`, `br"…"`; every escape Rust
    accepts in a string, including `\\` + newline (drop the newline and the
    following whitespace — `EXPECTED_ALTDEBUG_F` is written that way)."""
    i = at
    n = len(text)
    if i < n and text[i] == "b":
        i += 1
    if i < n and text[i] == "r":
        j = i + 1
        hashes = 0
        while j < n and text[j] == "#":
            hashes += 1
            j += 1
        if j < n and text[j] == '"':
            close = '"' + "#" * hashes
            k = text.find(close, j + 1)
            if k == -1:
                return None
            return text[j + 1:k], k + len(close)
        return None
    if i >= n or text[i] != '"':
        return None
    out: list[str] = []
    i += 1
    while i < n:
        ch = text[i]
        if ch == "\\":
            if i + 1 >= n:
                return None
            nxt = text[i + 1]
            if nxt == "\n" or nxt == "\r":
                i += 2
                while i < n and text[i] in " \t\r\n":
                    i += 1
                continue
            if nxt == "x" and i + 3 < n:
                try:
                    out.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            if nxt == "u" and text.startswith("{", i + 2):
                brace_end = text.find("}", i + 3)
                if brace_end != -1:
                    try:
                        out.append(chr(int(text[i + 3:brace_end], 16)))
                        i = brace_end + 1
                        continue
                    except ValueError:
                        pass
            out.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None


def _string_opener_at(text: str, i: int) -> bool:
    """Is a string literal (plain, byte or raw) opening at `i`?"""
    c = text[i]
    if c == '"':
        return True
    if c not in "br" or (i > 0 and _is_ident_char(text[i - 1])):
        return False
    j = i
    if text[j] == "b":
        j += 1
    if j < len(text) and text[j] == "r":
        j += 1
        while j < len(text) and text[j] == "#":
            j += 1
    return j < len(text) and text[j] == '"'


def _char_literal_end(text: str, i: int) -> int | None:
    """Index past the char literal at `i` (a `'`), or None for a lifetime."""
    n = len(text)
    if i + 1 >= n:
        return None
    if text[i + 1] == "\\":
        j = text.find("'", i + 2)
        return j + 1 if j != -1 else None
    if i + 2 < n and text[i + 2] == "'":
        return i + 3
    return None


def _blank_comments(text: str) -> str:
    """`text` with every `//` and `/* */` comment replaced by spaces (offsets
    preserved), string and char literals left intact."""
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        if _string_opener_at(text, i):
            lit = _read_string_literal(text, i)
            i = lit[1] if lit else i + 1
            continue
        if text[i] == "'":
            end = _char_literal_end(text, i)
            i = end if end else i + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                out[k] = " " if out[k] != "\n" else "\n"
            i = j
            continue
        i += 1
    return "".join(out)


def _skip_literal(text: str, i: int) -> int:
    """Index past a string/char literal opening at `i`, or `i` if none opens."""
    if _string_opener_at(text, i):
        lit = _read_string_literal(text, i)
        return lit[1] if lit else i + 1
    if text[i] == "'":
        end = _char_literal_end(text, i)
        return end if end else i + 1
    return i


def _matching(text: str, open_at: int, open_: str, close: str) -> int | None:
    """Index of the delimiter closing the pair opened at `open_at`, string-aware."""
    depth = 0
    i = open_at
    n = len(text)
    while i < n:
        j = _skip_literal(text, i)
        if j != i:
            i = j
            continue
        c = text[i]
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top_level(args: str) -> list[str]:
    """`args` split at depth-0 commas (parens/brackets/braces, string-aware)."""
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
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
        elif c == "," and depth == 0:
            parts.append(args[start:i].strip())
            start = i + 1
        i += 1
    tail = args[start:].strip()
    if tail or parts:
        parts.append(tail)
    return parts


def _literals_in(expr: str) -> list[str]:
    """Every string literal value in `expr`, in order."""
    out: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        if _string_opener_at(expr, i):
            lit = _read_string_literal(expr, i)
            if lit is None:
                i += 1
                continue
            out.append(lit[0])
            i = lit[1]
            continue
        if expr[i] == "'":
            end = _char_literal_end(expr, i)
            i = end if end else i + 1
            continue
        i += 1
    return out


def _idents_in(expr: str) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        j = _skip_literal(expr, i)
        if j != i:
            i = j
            continue
        if _is_ident_char(expr[i]) and not expr[i].isdigit():
            k = i
            while k < n and _is_ident_char(expr[k]):
                k += 1
            out.append(expr[i:k])
            i = k
            continue
        i += 1
    return out


# --- the block's shape ---------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _TestItem:
    name: str
    attrs: str      # the attribute lines between `#[test]` and the `fn`
    body: str       # the fn's braced body, comments blanked


@dataclass(frozen=True, slots=True)
class _AssertCall:
    callee: str     # `assert_eq`, `assert`, `assert_display`, …
    args: str       # the text between its parentheses


def _test_items(block: str) -> list[_TestItem]:
    text = _blank_comments(block)
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    items: list[_TestItem] = []
    i = 0
    while i < len(lines):
        if not is_test_attr_line(lines[i]):
            i += 1
            continue
        j = i + 1
        attrs: list[str] = []
        name = ""
        while j < len(lines):
            nxt = lines[j].strip()
            if not nxt or nxt.startswith("//"):
                j += 1
                continue
            if nxt.startswith("#"):
                attrs.append(nxt)
                j += 1
                continue
            name = fn_name_on_line(lines[j])
            break
        if name:
            brace = text.find("{", offsets[j])
            close = _matching(text, brace, "{", "}") if brace != -1 else None
            body = text[brace:close + 1] if (brace != -1 and close is not None) else ""
            items.append(_TestItem(name, "\n".join(attrs), body))
        i = j + 1
    return items


def _assert_calls(body: str) -> list[_AssertCall]:
    """Every `assert…!(…)` macro and `assert…(…)` call in `body`, outermost
    first (nested asserts are also reported on their own)."""
    out: list[_AssertCall] = []
    i = 0
    n = len(body)
    while i < n:
        j = _skip_literal(body, i)
        if j != i:
            i = j
            continue
        if body.startswith("assert", i) and (i == 0 or not _is_ident_char(body[i - 1])):
            k = i
            while k < n and _is_ident_char(body[k]):
                k += 1
            callee = body[i:k]
            m = k
            if m < n and body[m] == "!":
                m += 1
            while m < n and body[m].isspace():
                m += 1
            if m < n and body[m] in "([":
                close = _matching(body, m, body[m], ")" if body[m] == "(" else "]")
                if close is not None:
                    out.append(_AssertCall(callee, body[m + 1:close]))
            i = k
            continue
        i += 1
    return out


def _bound_literals(block: str) -> dict[str, str]:
    """`const`/`static`/`let` names bound to a single string literal anywhere in
    the block — the shape `tests/test_fmt.rs` uses for its expected renderings."""
    text = _blank_comments(block)
    out: dict[str, str] = {}
    n = len(text)
    for kw in ("const", "static", "let"):
        at = 0
        while (at := text.find(kw, at)) >= 0:
            before = text[at - 1] if at else " "
            after = text[at + len(kw):at + len(kw) + 1] or " "
            at += len(kw)
            if _is_ident_char(before) or after not in " \t":
                continue
            p = at
            while p < n and text[p].isspace():
                p += 1
            if text.startswith("mut ", p):
                p += 4
            s = p
            while p < n and _is_ident_char(text[p]):
                p += 1
            name = text[s:p]
            if not name:
                continue
            eq = text.find("=", p)
            semi = text.find(";", p)
            if eq == -1 or (semi != -1 and semi < eq):
                continue
            q = eq + 1
            while q < n and text[q].isspace():
                q += 1
            lit = _read_string_literal(text, q) if q < n else None
            if lit is None:
                continue
            rest = text[lit[1]:semi if semi != -1 else n].strip()
            if rest in ("", ".to_string()", ".to_owned()", ".into()"):
                out[name] = lit[0]
    return out


def _unwrap_arg(arg: str) -> str:
    """`arg` stripped of the wrappers that leave a literal's VALUE intact:
    `&`, `String::from(…)`, `format!(…)`, `.to_string()`, `.to_owned()`,
    `.into()`, `.as_str()`, and one set of parentheses."""
    a = arg.strip()
    changed = True
    while changed:
        changed = False
        while a.startswith("&"):
            a = a[1:].lstrip()
            changed = True
        for head in ("String::from(", "format!(", "format!["):
            if a.startswith(head) and a.endswith(")" if head.endswith("(") else "]"):
                a = a[len(head):-1].strip()
                changed = True
        for tail in (".to_string()", ".to_owned()", ".into()", ".as_str()"):
            if a.endswith(tail):
                a = a[:-len(tail)].strip()
                changed = True
        if a.startswith("(") and a.endswith(")") and _matching(a, 0, "(", ")") == len(a) - 1:
            a = a[1:-1].strip()
            changed = True
    return a


def _exact_value_of(arg: str, bound: dict[str, str]) -> str | None:
    """The literal value `arg` IS (after unwrapping), or None when it is an
    expression — `e.to_string()` is not a value, `"x".to_string()` is."""
    a = _unwrap_arg(arg)
    lit = _read_string_literal(a, 0)
    if lit is not None and lit[1] == len(a):
        return lit[0]
    if a in bound:
        return bound[a]
    return None


def _comparison_operands(arg: str) -> list[str]:
    """Operands of a depth-0 `==` / `!=` inside `arg`, else []."""
    depth = 0
    i = 0
    n = len(arg)
    cuts: list[int] = []
    while i < n:
        j = _skip_literal(arg, i)
        if j != i:
            i = j
            continue
        c = arg[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and (arg.startswith("==", i) or arg.startswith("!=", i)):
            cuts.append(i)
            i += 2
            continue
        i += 1
    if not cuts:
        return []
    parts: list[str] = []
    start = 0
    for cut in cuts:
        parts.append(arg[start:cut].strip())
        start = cut + 2
    parts.append(arg[start:].strip())
    return parts


def _pinned_string_values(item: _TestItem, bound: dict[str, str]) -> set[str]:
    """Every string value `item` asserts EXACTLY (see the module docstring for
    which positions count)."""
    values: set[str] = set()
    for call in _assert_calls(item.body):
        args = _split_top_level(call.args)
        if call.callee in ("assert_eq", "assert_ne"):
            for arg in args[:2]:
                v = _exact_value_of(arg, bound)
                if v is not None:
                    values.add(v)
        elif call.callee != "assert":
            for arg in args:
                v = _exact_value_of(arg, bound)
                if v is not None:
                    values.add(v)
        for arg in args:
            for operand in _comparison_operands(arg):
                v = _exact_value_of(operand, bound)
                if v is not None:
                    values.add(v)
    return values


def _should_panic_expected(item: _TestItem) -> str | None:
    at = item.attrs.find("should_panic")
    if at == -1:
        return None
    eq = item.attrs.find("expected", at)
    if eq == -1:
        return None
    q = item.attrs.find('"', eq)
    if q == -1:
        return None
    lit = _read_string_literal(item.attrs, q)
    return lit[0] if lit else None


def _normalize_type(t: str) -> str:
    s = "".join(t.split())
    for p in _STD_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return s


def _same_type(a: str, b: str) -> bool:
    """`a` and `b` name the same type: equal after normalization, or one is a
    `::`-suffix of the other with at least two segments (`io::Error` matches
    `std::io::Error`; a bare `Error` matches nothing longer than itself)."""
    x, y = _normalize_type(a), _normalize_type(b)
    if x == y:
        return True
    short, long_ = (x, y) if len(x) < len(y) else (y, x)
    return short.count("::") >= 1 and long_.endswith("::" + short)


@dataclass(frozen=True, slots=True)
class _IdentityAssert:
    type_text: str
    positive: bool


def _identity_asserts(item: _TestItem) -> list[_IdentityAssert]:
    out: list[_IdentityAssert] = []
    for call in _assert_calls(item.body):
        args = _split_top_level(call.args)
        flip_all = call.callee == "assert_ne" or (
            call.callee == "assert_eq" and any(a.strip() == "false" for a in args[:2]))
        for arg in args:
            i = 0
            n = len(arg)
            while i < n:
                hit = None
                for m in _IDENTITY_METHODS:
                    needle = "." + m + "::<"
                    if arg.startswith(needle, i):
                        hit = needle
                        break
                if hit is None:
                    i += 1
                    continue
                open_at = i + len(hit) - 1
                close = _matching(arg, open_at, "<", ">")
                if close is None:
                    break
                type_text = arg[open_at + 1:close]
                tail = arg[close + 1:]
                if tail.startswith("("):
                    paren_close = _matching(tail, 0, "(", ")")
                    tail = tail[paren_close + 1:] if paren_close is not None else tail
                tail_s = tail.lstrip()
                negated = arg.lstrip().startswith("!")
                if any(tail_s.startswith(t) for t in _NEGATING_TAILS) or _has_negative_pattern(tail_s):
                    negated = not negated
                if flip_all:
                    negated = not negated
                out.append(_IdentityAssert(type_text, not negated))
                i = close + 1
    return out


def _has_negative_pattern(tail: str) -> bool:
    """`matches!(x.downcast_ref::<T>(), None)` and `…, Err(_))` read as a
    negative identity claim."""
    for word in ("None", "Err"):
        at = 0
        while (at := tail.find(word, at)) >= 0:
            before = tail[at - 1] if at else " "
            after = tail[at + len(word):at + len(word) + 1] or " "
            at += len(word)
            if _is_ident_char(before) or _is_ident_char(after):
                continue
            if word == "None" or after == "(":
                return True
    return False


# --- public API -----------------------------------------------------------------

def _kind_name(spec: BehaviourSpec) -> str:
    return spec.then.kind.name if spec.then is not None else ""


def is_pinnable(spec: BehaviourSpec) -> bool:
    """Can a unit test pin this behaviour by an exact assertion at all? String
    kinds with a value of at least MIN_PINNABLE_VALUE_CHARS, and type identity.
    ASSERTION-subkind specs (no `then`), chains, bare downcast booleans and the
    compile-time layout pins are not the unit oracle's job."""
    if spec.then is None:
        return False
    kind = _kind_name(spec)
    if kind == _IDENTITY_KIND:
        return bool(spec.then.value.strip())
    return kind in _STRING_KINDS and len(spec.then.value) >= MIN_PINNABLE_VALUE_CHARS


def pinnable_specs(specs: Iterable[BehaviourSpec]) -> tuple[BehaviourSpec, ...]:
    return tuple(s for s in specs if is_pinnable(s))


def behaviours_pinned_by(block_text: str, specs: Iterable[BehaviourSpec]) -> frozenset[str]:
    """Ids of the specs some `#[test]` item in `block_text` pins exactly."""
    wanted = pinnable_specs(specs)
    if not wanted or not block_text.strip():
        return frozenset()
    bound = _bound_literals(block_text)
    items = _test_items(block_text)
    if not items:
        return frozenset()
    string_values: set[str] = set()
    identities: list[_IdentityAssert] = []
    panics: set[str] = set()
    for item in items:
        string_values |= _pinned_string_values(item, bound)
        identities.extend(_identity_asserts(item))
        expected = _should_panic_expected(item)
        if expected:
            panics.add(expected)
    stripped = {v.rstrip("\n") for v in string_values}
    out: set[str] = set()
    for spec in wanted:
        assert spec.then is not None
        kind = _kind_name(spec)
        if kind == _IDENTITY_KIND:
            if any(_same_type(a.type_text, spec.then.value) and a.positive == spec.then.ok
                   for a in identities):
                out.add(spec.behaviour_id)
            continue
        value = spec.then.value
        if (value in string_values or value.rstrip("\n") in stripped
                or (kind == "PANIC_MESSAGE" and value in panics)):
            out.add(spec.behaviour_id)
    return frozenset(out)


def _is_literal_arg(arg: str, bound: dict[str, str]) -> bool:
    """A string / char / numeric literal, or a name bound to one — never a
    boolean (`assert_eq!(x.is_ok(), true)` is `is_ok` in disguise)."""
    a = _unwrap_arg(arg)
    if not a or a in _TRIVIAL_ARGS:
        return False
    if _exact_value_of(a, bound) is not None:
        return True
    if a.startswith("'"):
        return _char_literal_end(a, 0) == len(a)
    core = a.replace("_", "")
    if a[0].isdigit() or (a[0] == "-" and len(a) > 1 and a[1].isdigit()):
        return all(c.isalnum() or c in ".-" for c in core)
    return False


_TRIVIAL_PATTERNS = frozenset({"_", "Some(_)", "Ok(_)", "Err(_)", "None", "Some(..)", "Ok(..)", "Err(..)"})


def _matches_is_exact(expr: str) -> bool:
    """`matches!(x, Pattern)` inside `expr` with a pattern that names a value —
    `MathError::DivByZero`, `Some(3)` — rather than a shape (`Some(_)`, `_`)."""
    at = expr.find("matches!")
    if at == -1:
        return False
    open_at = expr.find("(", at)
    close = _matching(expr, open_at, "(", ")") if open_at != -1 else None
    if close is None:
        return False
    args = _split_top_level(expr[open_at + 1:close])
    if len(args) < 2:
        return False
    return "".join(args[1].split()) not in _TRIVIAL_PATTERNS


def weak_assertion_reason(block_text: str) -> str | None:
    """None when at least one test in the block asserts an exact value; else a
    reason naming the assertion shapes the block does use."""
    bound = _bound_literals(block_text)
    items = _test_items(block_text)
    shapes: list[str] = []
    for item in items:
        if _should_panic_expected(item):
            return None
        calls = _assert_calls(item.body)
        for call in calls:
            args = _split_top_level(call.args)
            if call.callee in ("assert_eq", "assert_ne"):
                cmp = args[:2]
                if len(cmp) == 2 and not any(a.strip() in _TRIVIAL_ARGS for a in cmp):
                    return None
                if any(_is_literal_arg(a, bound) for a in cmp):
                    return None
                shapes.append(f"{call.callee}!(…, {[a for a in cmp if a.strip() in _TRIVIAL_ARGS][0]})"
                              if any(a.strip() in _TRIVIAL_ARGS for a in cmp) else f"{call.callee}!(…)")
                continue
            if call.callee == "assert":
                first = args[0] if args else ""
                if _comparison_operands(first):
                    return None
                if any("." + m + "::<" in first for m in _IDENTITY_METHODS):
                    return None
                if _matches_is_exact(first):
                    return None
                shape = next((p for p in _WEAK_PREDICATES if p in first), None)
                shapes.append(f"assert!({shape}…)" if shape else
                              ("assert!(matches!(…, _))" if "matches!" in first else "assert!(…)"))
                continue
            if any(_is_literal_arg(a, bound) for a in args):
                return None
            shapes.append(f"{call.callee}(…)")
        if not calls:
            body_shapes = [p for p in (".unwrap()", ".expect(") if p in item.body]
            shapes.append(f"{item.name}: " + ("/".join(body_shapes) + " without an assert"
                                              if body_shapes else "no assertion"))
    if not items:
        return "no #[test] item"
    seen: list[str] = []
    for s in shapes:
        if s not in seen:
            seen.append(s)
    return "no exact-value assertion — only " + ", ".join(seen)


def _matches_file(spec: BehaviourSpec, fn_names: Collection[str], pub_items: Collection[str]) -> bool:
    for e in spec.exercises:
        if e in fn_names or any(seg in fn_names for seg in e.split("::")):
            return True
    if not pub_items:
        return False
    words = set(_idents_in(example_code(spec)))
    return any(p in words for p in pub_items)


def select_behaviours_for_file(
    specs: Iterable[BehaviourSpec], *,
    file_fn_names: Collection[str],
    file_pub_items: Collection[str],
    already_pinned: Collection[str],
) -> tuple[BehaviourSpec, ...]:
    """The UNPINNED, pinnable specs relevant to one file: `exercises` names a fn
    of the file (any `::` segment), or the example's code
    (:func:`example_code`) uses a pub item of the file as a whole word."""
    pinned = set(already_pinned)
    fns = set(file_fn_names)
    pubs = set(file_pub_items)
    return tuple(s for s in pinnable_specs(specs)
                 if s.behaviour_id not in pinned and _matches_file(s, fns, pubs))


def _rust_literal(value: str) -> str:
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\t", "\\t")) + '"'


def _render_one(spec: BehaviourSpec) -> str:
    assert spec.then is not None
    # No class label: `BehaviourClass` is the verifier's category (how an
    # observable is compared), internal to census and ranking. What the model
    # needs is the rule the example illustrates, rendered next.
    lines = [f"- {spec.behaviour_id}"]
    # The rule first: the test is asked to pin an EXAMPLE of it, and a
    # generated test that pins the rule's other cases is worth more than a
    # copy of this one. The setup is the slice the when needs (never the
    # whole harvested test, which this line used to squash to 400 chars).
    for rule in spec.rules:
        lines.append("  rule: " + " ".join(rule.sentence.split()))
    if spec.setup.strip():
        lines.append("  setup: " + " ".join(spec.setup.split())[:400])
    when = " ".join(spec.when.split())[:400]
    lines.append(f"  when: {spec.via}({when}, …)" if spec.via else "  when: " + when)
    if _kind_name(spec) == _IDENTITY_KIND:
        lines.append(f"  then: `.is::<{spec.then.value}>()` is {'true' if spec.then.ok else 'false'}"
                     f" (equivalently `.downcast_ref::<{spec.then.value}>()`"
                     f" is {'Some' if spec.then.ok else 'None'})")
    elif "\n" in spec.then.value:
        lines.append("  then (exact, multi-line — assert_eq! against the whole text):")
        lines.append("  ```")
        lines.extend("  " + ln for ln in spec.then.value.split("\n"))
        lines.append("  ```")
    else:
        lines.append("  then: " + _rust_literal(spec.then.value))
    return "\n".join(lines)


def render_behaviour_list(
    specs: Iterable[BehaviourSpec], *, cap_chars: int = 6000, rel_path: str = "",
) -> str:
    """The listed behaviours as one prompt section, "" when there are none."""
    items = list(specs)
    if not items:
        return ""
    where = f" of `{rel_path}`" if rel_path else ""
    header = (f"\nHarvested behaviours{where} NOT yet pinned by any generated test — "
              f"each test you write must pin at least one of them by asserting the "
              f"EXACT `then` (whole value, `assert_eq!`; for a type identity, the "
              f"`is::<T>()` / `downcast_ref::<T>()` form):\n")
    body: list[str] = []
    used = len(header)
    shown = 0
    for spec in items:
        chunk = _render_one(spec) + "\n"
        if used + len(chunk) > cap_chars and shown:
            break
        body.append(chunk)
        used += len(chunk)
        shown += 1
    omitted = len(items) - shown
    tail = f"(+{omitted} more behaviour(s) not shown)\n" if omitted else ""
    return header + "".join(body) + tail


def render_behaviours_for_file(
    specs: Iterable[BehaviourSpec], *,
    file_fn_names: Collection[str],
    file_pub_items: Collection[str],
    already_pinned: Collection[str],
    cap_chars: int = 6000,
    rel_path: str = "",
) -> str:
    """`select_behaviours_for_file` rendered by `render_behaviour_list`."""
    return render_behaviour_list(
        select_behaviours_for_file(specs, file_fn_names=file_fn_names,
                                   file_pub_items=file_pub_items,
                                   already_pinned=already_pinned),
        cap_chars=cap_chars, rel_path=rel_path)
