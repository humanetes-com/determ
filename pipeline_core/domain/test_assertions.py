"""Test-assertion harvester — the observables a crate's
own `tests/*.rs` pin, lifted as OUTCOME behaviours with a runtime Then.

Why this tier exists. The bake's suite for anyhow (30 specs, 2026-08-26)
held 24 doc-fence ASSERTION specs whose `then` is null by invariant and 6
compile-time layout pins — not one runtime observable — while the three
observables the 98-tree failures pin all sit in integration tests:
`bail_literal().unwrap_err().downcast::<&str>().unwrap()` (tests/test_downcast.rs),
`assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", f().unwrap_err()))` with a
multi-line `const … = "\\` continuation literal (tests/test_fmt.rs:39-93),
and `assert_err(test, "Condition failed: …")` through a file-local helper
(tests/test_ensure.rs:78). A harvest that reads doc fences only cannot see
any of them. This one reads each `#[test]` item and records:

  (a) `assert_eq!(X, LIT)` / `assert_eq!(LIT, X)` — LIT a string literal or a
      `const NAME: &str` of the same file, evaluated EXACTLY (escapes and the
      `\\<newline>` continuation); kind by X's text: ALT_DEBUG for `{:#?}`,
      DEBUG for `{:?}`, DISPLAY for `.to_string()` / `{}` / `{:#}` / `{x}`,
      else RETURN_RENDERED.
  (b) identity — `assert!(v.is::<T>())` / `assert!(!v.is::<T>())`,
      `v.downcast_ref::<T>().is_some()/.is_none()`, `v.downcast::<T>().unwrap()`
      (holds) / `.is_err()` (refused) → TYPE_IDENTITY(T, ok).
  (c) helper-mediated — `<ident>(…, LIT)` where ident starts with `assert` or
      is a fn declared in the same file whose body asserts → DISPLAY(LIT),
      `when` = the call's first argument text.

Legality: every record is an OBSERVABLE the test already asserts — the same
class as the assertion itself, never a body (Hard Rule 11). The pipeline's
own generated tests (`*.gen_tests_*.rs` siblings and `tests/{stem}_gen_{N}.rs`
integration files) are refused by name: they were written against a tree
the reconstruction is free to redesign, and harvesting them would pin the
suite to itself.

Scanning is structural (bracket depth, string/char literals, comments), not
regex: the arguments of `assert_eq!` span lines and carry nested calls,
turbofish generics and escaped quotes.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    ExampleOrigin,
    FenceDirective,
    ObservableOutcome,
    OriginKind,
    OutcomeKind,
    classified,
)
from pipeline_core.domain.example_setup import (
    IMPORTS_KEY_SUFFIX,
    helper_closure,
    item_index,
    mod_declarations,
    setup_slice,
    use_block,
)
from pipeline_core.domain.generation import is_claimed_pipeline_gen_test
from pipeline_core.domain.refill_evidence import test_item_source
from pipeline_core.domain.rust_idents import fn_decl_names
from pipeline_core.domain.rust_scan import attributed_test_fn_names

# Sibling-name marker kept for callers that skip `*.gen_tests_*.rs` by
# substring. Integration files use `{stem}_gen_{N}.rs` — see
# `is_pipeline_gen_test_name`.
GEN_TESTS_MARKER = ".gen_tests_"

_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_OPENERS = "([{"
_CLOSERS = ")]}"
_MATCH = {")": "(", "]": "[", "}": "{"}
_IDENTITY_METHODS = ("is", "downcast", "downcast_ref", "downcast_mut")
_ASSERT_MACROS = ("assert_eq!", "assert!", "assert_ne!")
_KEYWORDS = frozenset({"if", "while", "match", "for", "return", "let", "loop",
                       "Some", "Ok", "Err", "Box", "Vec", "String"})


# --- Rust literal reading ----------------------------------------------------

def parse_string_literal(text: str, i: int) -> tuple[str, int] | None:
    """`(value, end)` for the Rust string literal starting at `text[i]` — a
    plain `"…"`, byte `b"…"` or raw `r#"…"#` — or None when nothing starts
    there. Escapes are evaluated as rustc does: `\\n \\r \\t \\0 \\\\ \\" \\'`,
    `\\xNN`, `\\u{…}`, and the line-continuation `\\<newline>` which drops the
    newline AND all leading whitespace of the following lines (how
    tests/test_fmt.rs writes its multi-line `EXPECTED_*` consts)."""
    n = len(text)
    j = i
    if j < n and text[j] == "b":
        j += 1
    if j < n and text[j] == "r":
        j += 1
        hashes = 0
        while j < n and text[j] == "#":
            hashes += 1
            j += 1
        if j >= n or text[j] != '"':
            return None
        j += 1
        close = '"' + "#" * hashes
        end = text.find(close, j)
        if end < 0:
            return None
        return text[j:end], end + len(close)
    if j >= n or text[j] != '"':
        return None
    j += 1
    out: list[str] = []
    while j < n:
        c = text[j]
        if c == '"':
            return "".join(out), j + 1
        if c != "\\":
            out.append(c)
            j += 1
            continue
        j += 1
        if j >= n:
            return None
        e = text[j]
        if e == "n":
            out.append("\n")
        elif e == "t":
            out.append("\t")
        elif e == "r":
            out.append("\r")
        elif e == "0":
            out.append("\0")
        elif e in ("\\", '"', "'"):
            out.append(e)
        elif e == "x":
            out.append(chr(int(text[j + 1:j + 3], 16)))
            j += 2
        elif e == "u" and j + 1 < n and text[j + 1] == "{":
            end = text.find("}", j + 2)
            if end < 0:
                return None
            out.append(chr(int(text[j + 2:end], 16)))
            j = end
        elif e in ("\n", "\r"):
            while j < n and text[j].isspace():
                j += 1
            continue
        else:
            out.append(e)
        j += 1
    return None


def _skip_char_literal(text: str, i: int) -> int:
    """Index just past the char literal at `text[i] == "'"`, or `i + 1` when
    the quote opens a lifetime (`'static`) instead."""
    n = len(text)
    if i + 1 >= n:
        return i + 1
    if text[i + 1] == "\\":
        end = text.find("'", i + 2)
        return n if end < 0 else end + 1
    if i + 2 < n and text[i + 2] == "'":
        return i + 3
    return i + 1


def _skip_comment(text: str, i: int) -> int | None:
    """Index just past the comment opening at `i`, or None if none opens."""
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return len(text) if end < 0 else end + 2
    return None


@dataclass(frozen=True, slots=True)
class _Scan:
    """Structural facts about one text: which offsets are inside a literal or
    comment (`opaque`), and the bracket pairs (`open_of`: close → open)."""

    opaque: tuple[bool, ...]
    open_of: dict[int, int]
    close_of: dict[int, int]


def _scan(text: str) -> _Scan:
    n = len(text)
    opaque = [False] * n
    stack: list[int] = []
    open_of: dict[int, int] = {}
    close_of: dict[int, int] = {}
    i = 0
    while i < n:
        c = text[i]
        if c == '"' or (c in "br" and i + 1 < n and text[i + 1] in "\"#r" and
                        (i == 0 or text[i - 1] not in _IDENT_CHARS)):
            lit = parse_string_literal(text, i)
            if lit is not None:
                for k in range(i, lit[1]):
                    opaque[k] = True
                i = lit[1]
                continue
            if c != '"':
                i += 1
                continue
            # unterminated: everything after is opaque
            for k in range(i, n):
                opaque[k] = True
            break
        if c == "'":
            end = _skip_char_literal(text, i)
            if end > i + 1:
                for k in range(i, end):
                    opaque[k] = True
            i = end
            continue
        if (cend := _skip_comment(text, i)) is not None:
            for k in range(i, cend):
                opaque[k] = True
            i = cend
            continue
        if c in _OPENERS:
            stack.append(i)
        elif c in _CLOSERS:
            while stack and text[stack[-1]] != _MATCH[c]:
                stack.pop()
            if stack:
                o = stack.pop()
                open_of[i] = o
                close_of[o] = i
        i += 1
    return _Scan(tuple(opaque), open_of, close_of)


def _split_top_level(text: str) -> list[str]:
    """`text` split on commas at bracket depth 0. Turbofish angle groups
    (`::<…>`) count as brackets; a bare `<` (a comparison) does not.
    Literals and comments are skipped. A trailing empty argument (the
    trailing comma rustfmt leaves) is dropped."""
    sc = _scan(text)
    out: list[str] = []
    depth = 0
    angle = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        if sc.opaque[i]:
            i += 1
            continue
        c = text[i]
        if c in _OPENERS:
            depth += 1
        elif c in _CLOSERS:
            depth -= 1
        elif c == "<" and (angle > 0 or text.startswith("::", max(0, i - 2))):
            angle += 1
        elif c == ">" and angle > 0 and not text.startswith("->", i - 1):
            angle -= 1
        elif c == "," and depth == 0 and angle == 0:
            out.append(text[start:i])
            start = i + 1
        i += 1
    out.append(text[start:])
    args = [a.strip() for a in out]
    if args and not args[-1]:
        args.pop()
    return args


def _is_ident(s: str) -> bool:
    return bool(s) and all(c in _IDENT_CHARS for c in s) and not s[0].isdigit()


def _literal_value(arg: str, consts: dict[str, str]) -> str | None:
    """The evaluated string when `arg` IS a string literal (nothing else on
    it) or names a same-file `const … &str`; None otherwise."""
    a = arg.strip()
    if not a:
        return None
    if a[0] == '"' or a.startswith(('b"', 'r"', "r#", 'br"', "br#")):
        lit = parse_string_literal(a, 0)
        if lit is not None and lit[1] == len(a):
            return lit[0]
        return None
    if _is_ident(a):
        return consts.get(a)
    return None


def string_consts(src: str) -> dict[str, str]:
    """`NAME → value` for every `const`/`static NAME: &str = "…";` (also
    `&'static str`) declared in `src`, literal evaluated exactly."""
    out: dict[str, str] = {}
    sc = _scan(src)
    for kw in ("const ", "static "):
        i = 0
        while (at := src.find(kw, i)) >= 0:
            i = at + len(kw)
            if sc.opaque[at] or (at > 0 and src[at - 1] in _IDENT_CHARS):
                continue
            j = i
            while j < len(src) and src[j].isspace():
                j += 1
            s = j
            while j < len(src) and src[j] in _IDENT_CHARS:
                j += 1
            name = src[s:j]
            if not name:
                continue
            colon = src.find(":", j)
            eq = src.find("=", j)
            if colon < 0 or eq < 0 or colon > eq:
                continue
            if "str" not in src[colon:eq]:
                continue
            k = eq + 1
            while k < len(src) and src[k].isspace():
                k += 1
            lit = parse_string_literal(src, k)
            if lit is not None:
                out[name] = lit[0]
    return out


# --- expression shape ---------------------------------------------------------

def _has_named_placeholder(x: str) -> bool:
    """`{ident}` inside `x` (`format!("{e}")`, `"{x:?}"` excluded here)."""
    i = 0
    while (at := x.find("{", i)) >= 0:
        j = at + 1
        while j < len(x) and x[j] in _IDENT_CHARS:
            j += 1
        if j > at + 1 and j < len(x) and x[j] == "}":
            return True
        i = at + 1
    return False


def outcome_kind_for(x: str) -> OutcomeKind:
    """The kind of observable `x` (the non-literal side of an `assert_eq!`)
    renders: ALT_DEBUG for `{:#?}`/`{v:#?}`, DEBUG for `{:?}`/`{v:?}`,
    DISPLAY for `.to_string()` / `{}` / `{:#}` / `{v}` / `to_owned()` on a
    str, else RETURN_RENDERED."""
    if "#?}" in x:
        return OutcomeKind.ALT_DEBUG_STRING
    if "?}" in x:
        return OutcomeKind.DEBUG_STRING
    if (".to_string()" in x or "{}" in x or "{:#}" in x
            or _has_named_placeholder(x)):
        return OutcomeKind.DISPLAY_STRING
    return OutcomeKind.RETURN_RENDERED


def _outcome(kind: OutcomeKind, value: str) -> ObservableOutcome:
    return ObservableOutcome(kind, value=value)


def _compact(text: str) -> str:
    """Whitespace collapsed; a method chain broken across lines re-joins
    (`x()\n    .y()` → `x().y()`)."""
    return " ".join(text.split()).replace(" .", ".")


def _macro_groups(item: str, sc: _Scan, macro: str) -> list[tuple[int, int, int]]:
    """`(name_at, open_at, close_at)` for every `macro(` call (`macro` includes
    the `!`), outside literals/comments, in order."""
    out: list[tuple[int, int, int]] = []
    i = 0
    while (at := item.find(macro, i)) >= 0:
        i = at + len(macro)
        if sc.opaque[at] or (at > 0 and item[at - 1] in _IDENT_CHARS):
            continue
        j = i
        while j < len(item) and item[j].isspace():
            j += 1
        if j >= len(item) or item[j] not in _OPENERS:
            continue
        close = sc.close_of.get(j)
        if close is None:
            continue
        out.append((at, j, close))
    return out


def _receiver_start(item: str, sc: _Scan, dot: int) -> int:
    """Start offset of the receiver expression of the method call whose `.`
    sits at `dot`: walks backwards over chained calls, paths, turbofish
    groups and macro invocations (`anyhow!("x").is::<T>()`), stopping at any
    operator/delimiter that cannot continue a postfix chain."""
    j = dot
    while True:
        k = j - 1
        while k >= 0 and item[k].isspace():
            k -= 1
        if k < 0:
            return j
        c = item[k]
        if c in _CLOSERS:
            o = sc.open_of.get(k)
            if o is None:
                return j
            j = o
            continue
        if c == ">":
            depth = 0
            m = k
            while m >= 0:
                if item[m] == ">":
                    depth += 1
                elif item[m] == "<":
                    depth -= 1
                    if depth == 0:
                        break
                m -= 1
            if m < 0 or not item.startswith("::", m - 2):
                return j
            j = m - 2
            continue
        if c == "?":
            j = k
            continue
        if c in _IDENT_CHARS:
            m = k
            while m >= 0 and item[m] in _IDENT_CHARS:
                m -= 1
            j = m + 1
            before = item[m] if m >= 0 else ""
            if before == ".":
                j = m
                continue
            if item.startswith("::", m - 1):
                j = m - 1
                continue
            return j
        if c == "!" and k > 0 and item[k - 1] in _IDENT_CHARS:
            # a macro's bang (`anyhow!("x")`), not a negation (`!err`)
            j = k
            continue
        return j


def _angle_group(item: str, lt: int) -> int:
    """Index just past the `<…>` group opening at `lt`, or -1."""
    depth = 0
    for i in range(lt, len(item)):
        if item[i] == "<":
            depth += 1
        elif item[i] == ">":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _inside_any(pos: int, groups: Iterable[tuple[int, int, int]]) -> bool:
    return any(o < pos < c for _, o, c in groups)


def _negated_before(item: str, start: int) -> bool:
    """A `!` immediately before `start` (whitespace and one `(` allowed)."""
    k = start - 1
    while k >= 0 and item[k].isspace():
        k -= 1
    if k >= 0 and item[k] == "(":
        k -= 1
        while k >= 0 and item[k].isspace():
            k -= 1
    return k >= 0 and item[k] == "!" and not (k > 0 and item[k - 1] in _IDENT_CHARS)


@dataclass(frozen=True, slots=True)
class _Found:
    pos: int
    when: str
    then: ObservableOutcome
    via: str = ""


def _find_assert_eq(item: str, sc: _Scan, consts: dict[str, str]) -> list[_Found]:
    out: list[_Found] = []
    for at, o, c in _macro_groups(item, sc, "assert_eq!"):
        args = _split_top_level(item[o + 1:c])
        if len(args) < 2:
            continue
        lhs, rhs = args[0], args[1]
        lv, rv = _literal_value(lhs, consts), _literal_value(rhs, consts)
        if (lv is None) == (rv is None):
            continue                      # both literal or neither
        value = lv if lv is not None else rv
        x = rhs if lv is not None else lhs
        assert value is not None
        out.append(_Found(at, _compact(x), _outcome(outcome_kind_for(x), value)))
    return out


def _find_identity(item: str, sc: _Scan) -> list[_Found]:
    out: list[_Found] = []
    asserts = _macro_groups(item, sc, "assert!")
    # An identity call inside `ensure!(…)` is the ensure's CONDITION, and the
    # test asserts the ensure FAILS — reading it as an identity assertion
    # inverts the truth (tests/test_ensure.rs: `ensure!(err.is::<&str>() ==
    # false)` where `err` IS a `&str`).
    ensures = _macro_groups(item, sc, "ensure!")
    i = 0
    while (dot := item.find(".", i)) >= 0:
        i = dot + 1
        if sc.opaque[dot]:
            continue
        j = dot + 1
        while j < len(item) and item[j] in _IDENT_CHARS:
            j += 1
        method = item[dot + 1:j]
        if method not in _IDENTITY_METHODS or not item.startswith("::<", j):
            continue
        end = _angle_group(item, j + 2)
        if end < 0:
            continue
        type_name = _compact(item[j + 3:end - 1])
        if not type_name:
            continue
        k = end
        while k < len(item) and item[k].isspace():
            k += 1
        if not item.startswith("()", k):
            continue
        call_end = k + 2
        if _inside_any(dot, ensures):
            continue
        rest = item[call_end:].lstrip()
        ok: bool | None = None
        if method == "is":
            if _inside_any(dot, asserts) and (rest.startswith(")") or rest.startswith(",")):
                ok = not _negated_before(item, _receiver_start(item, sc, dot))
        else:
            for suffix, holds in ((".unwrap()", True), (".expect(", True),
                                  (".is_some()", True), (".is_ok()", True),
                                  (".is_none()", False), (".is_err()", False)):
                if rest.startswith(suffix):
                    ok = holds
                    break
        if ok is None:
            continue
        start = _receiver_start(item, sc, dot)
        when = _compact(item[start:call_end])
        out.append(_Found(dot, when, ObservableOutcome.type_identity(type_name, ok)))
    return out


def _find_helper_calls(
    item: str, sc: _Scan, consts: dict[str, str], helpers: frozenset[str],
) -> list[_Found]:
    out: list[_Found] = []
    n = len(item)
    i = 0
    while i < n:
        if sc.opaque[i] or item[i] not in _IDENT_CHARS or item[i].isdigit():
            i += 1
            continue
        s = i
        while i < n and item[i] in _IDENT_CHARS:
            i += 1
        ident = item[s:i]
        before = item[s - 1] if s > 0 else ""
        if before == "." or item.startswith("::", s - 2) or before in _IDENT_CHARS:
            continue
        j = i
        while j < n and item[j].isspace():
            j += 1
        if j >= n or item[j] != "(":
            continue
        if ident in _KEYWORDS or not (ident.startswith("assert") or ident in helpers):
            continue
        close = sc.close_of.get(j)
        if close is None:
            continue
        args = _split_top_level(item[j + 1:close])
        if len(args) < 2:
            continue
        value = _literal_value(args[-1], consts)
        if value is None:
            continue
        out.append(_Found(s, _compact(args[0]), ObservableOutcome.display(value), via=ident))
    return out


def asserting_helper_names(src: str) -> frozenset[str]:
    """Fns declared in `src` whose item text asserts (`assert!`/`assert_eq!`/
    `assert_ne!`) — the helpers a test routes an expected literal through."""
    names: set[str] = set()
    for name in fn_decl_names(src):
        body = test_item_source(src, name)
        if body and any(m in body for m in _ASSERT_MACROS):
            names.add(name)
    return frozenset(names)


def _trim_item(item: str) -> str:
    """The item without the attribute/blank lines of the NEXT item that
    `test_item_source` carries at its tail."""
    lines = item.strip("\n").splitlines()
    while lines and (not lines[-1].strip() or lines[-1].strip().startswith("#[")):
        lines.pop()
    return "\n".join(lines)


def _sibling_files(source_file: str, test_src: str,
                   siblings: Mapping[str, str]) -> list[tuple[str, str]]:
    """`(path, text)` of the modules `test_src` declares with `mod NAME;`,
    looked up as `<dir>/NAME/mod.rs` or `<dir>/NAME.rs` among `siblings`."""
    base = source_file.rsplit("/", 1)[0] if "/" in source_file else ""
    out: list[tuple[str, str]] = []
    for name in mod_declarations(test_src):
        for cand in (f"{base}/{name}/mod.rs", f"{base}/{name}.rs"):
            cand = cand.lstrip("/")
            if cand in siblings:
                out.append((cand, siblings[cand]))
                break
    return out


def harvest_test_assertion_behaviours(
    test_src: str, *, source_file: str, cfg_cluster: str = "default",
    default_cluster: str | None = None,
    siblings: Mapping[str, str] | None = None,
) -> list[BehaviourSpec]:
    """OUTCOME examples, one per distinct observable each `#[test]` item in
    `test_src` asserts (forms (a)–(c) in the module docstring). Per item the
    findings are deduped by (when, then), ordered by source position and
    numbered; `source_ref` = `<file>::<test>`, id =
    `testassert::<file>::<test>::<i>`, origin `TEST(file, test, i)`,
    provenance HARVESTED, fence RUN, `klass` by `classify_behaviour`.

    `setup` is the def-use slice of the item before the assertion
    (`example_setup.setup_slice`), never the item: storing the item once per
    assertion was 193.8 KB of copies on the 2026-09-28 anyhow suite and
    handed prompts the whole test again. The file-level items the example
    calls — a fn outside the test, a type, the asserting helper (`via`) — are
    `helper_keys` with their text attached, resolved in this file and in the
    modules it declares with `mod NAME;` (looked up in `siblings`, the other
    test files by path); the suite stores each once.

    A `source_file` naming a pipeline-generated test (`*.gen_tests_*.rs`, or
    a `tests/{stem}_gen_{N}.rs` that carries the writer origin marker)
    yields nothing: those tests were authored against a tree the
    reconstruction is free to redesign. An authored file whose name merely
    parses as `{stem}_gen_{N}` is harvested — it is the crate's oracle.
    `default_cluster` is the crate's default cluster name for the CFG_WORLD
    class; None means "this cluster" (nothing is classified CFG_WORLD)."""
    name = source_file.rsplit("/", 1)[-1]
    if is_claimed_pipeline_gen_test(name, test_src):
        return []
    consts = string_consts(test_src)
    helpers = asserting_helper_names(test_src)
    index = item_index([(source_file, test_src),
                        *_sibling_files(source_file, test_src, siblings or {})])
    # The file's `use` lines are what its examples' names resolve to (the
    # crate's `Error` or std's, the crate's `Ok` or the prelude's); stored
    # once per file like any helper.
    imports = use_block(test_src)
    import_helper = ((f"{source_file}{IMPORTS_KEY_SUFFIX}", imports),) if imports else ()
    out: list[BehaviourSpec] = []
    seen_tests: set[str] = set()
    for test_name in attributed_test_fn_names(test_src):
        if test_name in seen_tests:
            continue
        seen_tests.add(test_name)
        item = _trim_item(test_item_source(test_src, test_name))
        if not item:
            continue
        sc = _scan(item)
        found = (_find_assert_eq(item, sc, consts)
                 + _find_identity(item, sc)
                 + _find_helper_calls(item, sc, consts, helpers - {test_name}))
        found.sort(key=lambda f: f.pos)
        seen: set[tuple[str, ObservableOutcome]] = set()
        i = 0
        for f in found:
            key = (f.when, f.then)
            if key in seen:
                continue
            seen.add(key)
            setup = setup_slice(item, f.pos, observers=helpers)
            resolved = import_helper + helper_closure(
                f"{setup}\n{f.when}", index, extra=(f.via,) if f.via else ())
            spec = BehaviourSpec(
                behaviour_id=f"testassert::{source_file}::{test_name}::{i}",
                setup=setup, when=f.when, then=f.then,
                provenance=BehaviourProvenance.HARVESTED,
                subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN,
                cfg_cluster=cfg_cluster,
                source_ref=f"{source_file}::{test_name}",
                origin=ExampleOrigin(OriginKind.TEST, source_file, test_name, i),
                via=f.via,
                helper_keys=tuple(k for k, _ in resolved),
                helpers=tuple(t for _, t in resolved))
            out.append(classified(spec, default_cluster=default_cluster or cfg_cluster))
            i += 1
    return out
