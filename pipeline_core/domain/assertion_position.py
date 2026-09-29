"""Where a failing test stopped, and the one rule every keep judges it by.

Why this module exists. The earlier-panic judgment used to order two panic
sites by line number whenever they sat in the same file. Its premise ("every
panic site is an assertion line in a test file, never src/") was measured on
a 13-failure tree (2026-08-21). On a tree with stubs it is false: run
`273278-22529e44` (2026-09-29, cluster `stable`) had 160 failure sites before
a fill and 142 after, every one of them an `unimplemented!()` line in `src/`.
Filling `context` let eleven tests reach the still-stubbed `ext_context`,
declared earlier in the same file (`src/context.rs` 43|50 -> 23). That is
progress, and line order graded it a regression: the sweep reverted ten
landings and the score fell from 37 to 19.

Line order is execution order only in one place: the straight-line body of
the failing test itself. It is not execution order for:

* a site in the crate under reconstruction, stub or not;
* a helper fn in the test file, declared above its callers;
* a closure defined before its call (`let t = || …; assert_err(t, …)`),
  even one written inside the test, whose line precedes the call;
* one test name in several binaries, when the sites are keyed by bare name
  and the first section wins.

So an :class:`AssertionPosition` exists only when the panic line is a line of
the failing test's OWN item body, outside every nested fn item and closure,
in the file of the binary that ran it; and it is keyed by binary plus test
name. Every other failure has no position, and two positions compare only
under the same key. `tests/test_fmt.rs` 91 -> 90 inside `test_altdebug`
(run `24951-a72c4642`, 2026-09-29) is still an earlier panic.

Backtrace frames were not chosen: sweeps do not set `RUST_BACKTRACE`, and the
crate's observables move with it, so enabling it would change the oracle's
world.

The decided rule (2026-09-29): a verdict outranks a position. Over an
attempt's attributed tests, any `lost` (passing -> failing) refuses;
`earlier` refuses only when nothing attributed `gained`; otherwise the
attempt stands (:class:`TestOutcomes`).
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from pipeline_core.domain.generation import parse_gen_tests_filename
from pipeline_core.domain.test_results import binary_output_sections

#: Reads an oracle test file by the path a panic printed (relative to the
#: package root). Returns None for any path that is not an oracle test file,
#: so a site inside the crate under reconstruction can never gain a position.
TestSource = Callable[[str], "str | None"]


@dataclass(frozen=True, slots=True, order=True)
class TestKey:
    """One test in one binary. A bare name is not a test: two integration
    binaries may each hold a `test_downcast_ref`."""

    __test__ = False  # domain VO, not a pytest class

    binary: str
    test: str


class PositionMove(NamedTuple):
    """A test whose position moved between two readings, under one key."""

    key: TestKey
    before: int
    after: int

    def render(self) -> str:
        return f"{self.key.test} {self.key.binary}:{self.before}->{self.after}"


Positions = Mapping[TestKey, int]


def leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


# --- the test item's own lines ----------------------------------------------

def _mask(src: str) -> str:
    """`src` with comments, string, byte-string, raw-string and char literals
    replaced by spaces, newlines kept, so every offset and line number is the
    source's and no bracket or `|` inside a literal is read as code."""
    out = list(src)
    n = len(src)
    i = 0

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
            continue
        prev_ident = i > 0 and (src[i - 1].isalnum() or src[i - 1] == "_")
        if c in "rb" and not prev_ident:
            raw = c == "r" or src.startswith("br", i)
            j = i + (2 if src.startswith("br", i) else 1)
            if raw:
                hashes = 0
                while j < n and src[j] == "#":
                    hashes += 1
                    j += 1
                if j < n and src[j] == '"':
                    close = '"' + "#" * hashes
                    k = src.find(close, j + 1)
                    k = n if k < 0 else k + len(close)
                    blank(i, k)
                    i = k
                    continue
            elif j < n and src[j] == '"':
                k = _string_end(src, j)
                blank(i, k)
                i = k
                continue
            elif j < n and src[j] == "'":
                k = _char_end(src, j)
                if k:
                    blank(i, k)
                    i = k
                    continue
        if c == '"':
            k = _string_end(src, i)
            blank(i, k)
            i = k
            continue
        if c == "'":
            k = _char_end(src, i)
            if k:
                blank(i, k)
                i = k
                continue
        i += 1
    return "".join(out)


def _string_end(src: str, quote: int) -> int:
    j = quote + 1
    while j < len(src):
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == '"':
            return j + 1
        j += 1
    return len(src)


def _char_end(src: str, quote: int) -> int:
    """End offset of a char literal opening at `quote`, or 0 for a lifetime
    (`'a` with no closing quote)."""
    j = quote + 1
    if j >= len(src):
        return 0
    if src[j] == "\\":
        k = src.find("'", j + 2)
        return k + 1 if 0 < k <= j + 12 else 0
    return j + 2 if j + 1 < len(src) and src[j + 1] == "'" else 0


def _match(masked: str, open_idx: int) -> int:
    """Offset of the bracket closing the one at `open_idx` (any of `([{`,
    counted together), or len(masked) when unbalanced."""
    depth = 0
    for k in range(open_idx, len(masked)):
        ch = masked[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return k
    return len(masked)


def _is_word_at(masked: str, idx: int, word: str) -> bool:
    end = idx + len(word)
    if masked[idx:end] != word:
        return False
    before_ok = idx == 0 or not (masked[idx - 1].isalnum() or masked[idx - 1] == "_")
    after_ok = end >= len(masked) or not (masked[end].isalnum() or masked[end] == "_")
    return before_ok and after_ok


def _fn_items(masked: str, name: str | None = None) -> list[tuple[int, int, int]]:
    """`(fn keyword offset, body open, body close)` of every fn item in
    `masked` (named `name` when given). A declaration with no body (a trait
    method signature) is skipped."""
    out: list[tuple[int, int, int]] = []
    i = masked.find("fn", 0)
    while i >= 0:
        if _is_word_at(masked, i, "fn"):
            j = i + 2
            while j < len(masked) and masked[j].isspace():
                j += 1
            k = j
            while k < len(masked) and (masked[k].isalnum() or masked[k] == "_"):
                k += 1
            ident = masked[j:k]
            if ident and (name is None or ident == name):
                paren = masked.find("(", k)
                if paren >= 0:
                    close = _match(masked, paren)
                    m = close + 1
                    while m < len(masked) and masked[m] not in "{;":
                        if masked[m] in "([":
                            m = _match(masked, m)
                        m += 1
                    if m < len(masked) and masked[m] == "{":
                        out.append((i, m, _match(masked, m)))
        i = masked.find("fn", i + 2)
    return out


#: Tokens after which a `|` opens a closure rather than an or-operator.
_CLOSURE_LEAD = "([{,=;>!&"
_CLOSURE_WORDS = ("move", "return", "async")


def _closures(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """`(start, end)` offsets of every closure inside `masked[start:end]`.

    A `|` opens a closure where an expression starts: after `(`, `[`, `{`,
    `,`, `=`, `;`, `>` (`=>`), `!`, `&`, or the words `move`, `return`,
    `async`. The closure's body is its brace block when the parameters are
    followed by one (optionally after `-> T`), otherwise the expression up to
    the `,` / `;` / closing bracket that ends it at its own depth."""
    out: list[tuple[int, int]] = []
    i = start
    while i < end:
        if masked[i] != "|":
            i += 1
            continue
        p = i - 1
        while p >= start and masked[p].isspace():
            p -= 1
        lead = p < start or masked[p] in _CLOSURE_LEAD or any(
            p - len(w) + 1 >= 0 and _is_word_at(masked, p - len(w) + 1, w)
            for w in _CLOSURE_WORDS)
        if not lead:
            i += 1
            continue
        if masked.startswith("||", i):
            params_end = i + 1
        else:
            params_end = masked.find("|", i + 1)
            if params_end < 0 or params_end >= end:
                break
        j = params_end + 1
        while j < end and masked[j].isspace():
            j += 1
        if masked.startswith("->", j):
            j = masked.find("{", j)
            j = end if j < 0 else j
        if j < end and masked[j] == "{":
            stop = _match(masked, j)
        else:
            stop, depth = j, 0
            while stop < end:
                ch = masked[stop]
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch in ",;" and depth == 0:
                    break
                stop += 1
        out.append((i, min(stop, end)))
        i = params_end + 1
    return out


def _line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1


def own_assertion_lines(file_src: str, test: str) -> frozenset[int] | None:
    """The 1-based lines of `fn {test}`'s body that execute in the test's
    own frame: the item body minus every nested fn item and closure (a line
    shared with one is excluded — its order against the call is unknown).

    None when the file defines no fn of that name, or more than one (a
    name in two modules of one file cannot be told apart here, so it has
    no position rather than a guessed one)."""
    if not file_src or not test:
        return None
    masked = _mask(file_src)
    items = _fn_items(masked, test)
    if len(items) != 1:
        return None
    _kw, open_, close = items[0]
    # The brace lines count: a one-line test body shares them.
    first, last = _line_of(file_src, open_), _line_of(file_src, close)
    excluded: set[int] = set()
    regions = [(kw, c) for kw, _o, c in _fn_items(masked[:close])
               if open_ < kw < close]
    regions += _closures(masked, open_ + 1, close)
    for a, b in regions:
        excluded.update(range(_line_of(file_src, a), _line_of(file_src, b) + 1))
    return frozenset(n for n in range(first, last + 1) if n not in excluded)


# --- positions of one capture -----------------------------------------------

def is_oracle_test_path(rel: str) -> bool:
    """A path a panic printed that names an oracle test file: `tests/**.rs`,
    or a generated test sibling under `src/`. Relative, no `..`: anything
    else — the crate under reconstruction above all — is never read for a
    position."""
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return False
    if rel.startswith("tests/"):
        return rel.endswith(".rs")
    return (rel.startswith("src/")
            and parse_gen_tests_filename(rel.rsplit("/", 1)[-1]) is not None)


def _file_of_binary(label: str, panic_file: str) -> bool:
    """Whether a panic printed at `panic_file` is in the source file of the
    binary labelled `label`: an integration binary's own file, or a
    generated test sibling of the lib's unit-test binary."""
    if not is_oracle_test_path(panic_file):
        return False
    if label == panic_file:
        return True
    return label.startswith("unittests ") and panic_file.startswith("src/")


def assertion_positions(capture: str, source_of: TestSource) -> dict[TestKey, int]:
    """Every :class:`TestKey` in `capture` whose failure has a position.

    Built per binary section BEFORE any collapse onto the bare name, so one
    name in two binaries yields two keys. `source_of` supplies the oracle
    file; a site it does not return (the crate's `src/`, a missing file) has
    no position."""
    # Imported here: `test_report` imports this module's model.
    from pipeline_core.domain.test_report import parse_test_output

    out: dict[TestKey, int] = {}
    cache: dict[tuple[str, str], frozenset[int] | None] = {}
    for sec in binary_output_sections(capture or ""):
        for f in parse_test_output(sec.text).failures:
            if not f.panic_file or f.panic_line is None:
                continue
            if not _file_of_binary(sec.label, f.panic_file):
                continue
            name = leaf(f.test_name)
            ck = (f.panic_file, name)
            if ck not in cache:
                src = source_of(f.panic_file)
                cache[ck] = own_assertion_lines(src, name) if src else None
            lines = cache[ck]
            if lines and f.panic_line in lines:
                out[TestKey(sec.label, name)] = f.panic_line
    return out


def positions_for(positions: Positions, tests: Collection[str]) -> dict[TestKey, int]:
    """`positions` restricted to the bare names in `tests`."""
    names = {leaf(t) for t in tests}
    return {k: v for k, v in positions.items() if k.test in names}


def compare_positions(
    before: Positions, after: Positions,
) -> tuple[tuple[PositionMove, ...], tuple[PositionMove, ...]]:
    """(earlier, later): keys present in both readings whose position moved.
    Two positions compare only under one key; a key in one reading alone is
    unmeasured and in neither list."""
    earlier: list[PositionMove] = []
    later: list[PositionMove] = []
    for key, b in sorted(before.items()):
        a = after.get(key)
        if a is None or a == b:
            continue
        (earlier if a < b else later).append(PositionMove(key, b, a))
    return tuple(earlier), tuple(later)


# --- the decided rule ---------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TestOutcomes:
    """Typed per-test outcomes of one attempt over its attributed tests.

    `gained`: failing before, passing after. `lost`: passing before, failing
    after. `earlier` / `later`: failing in both, with a position under the
    same key in both, that moved. `unpositioned`: failing in both with no
    comparable position.

    :attr:`refuses` is the decided rule, and the only one: any loss refuses;
    an earlier position refuses only when nothing gained. Every keep that
    compares an attempt to a baseline reads it — the ledger grade
    (`body_ledger.classify`), the refill keeps (`test_report.judge_refill`),
    the session's best state (`slice_session.judge_against_best`), the
    session turn and its peel, and the sweep's revert of its own revert."""

    __test__ = False

    gained: frozenset[str] = frozenset()
    lost: frozenset[str] = frozenset()
    earlier: tuple[PositionMove, ...] = ()
    later: tuple[PositionMove, ...] = ()
    unpositioned: frozenset[str] = frozenset()

    @property
    def refuses(self) -> bool:
        return bool(self.lost) or (bool(self.earlier) and not self.gained)

    @property
    def earlier_tests(self) -> tuple[str, ...]:
        return tuple(sorted({m.key.test for m in self.earlier}))

    @property
    def later_tests(self) -> tuple[str, ...]:
        return tuple(sorted({m.key.test for m in self.later}))


def outcomes_of(
    tests: Iterable[str], *,
    before_passed: Collection[str], before_failed: Collection[str],
    after_passed: Collection[str], after_failed: Collection[str],
    before_positions: Positions, after_positions: Positions,
) -> TestOutcomes:
    """The :class:`TestOutcomes` of `tests` (bare names) between two
    readings. Name sets are compared by bare name; positions by key."""
    names = {leaf(t) for t in tests}
    bp = {leaf(t) for t in before_passed}
    bf = {leaf(t) for t in before_failed}
    ap = {leaf(t) for t in after_passed}
    af = {leaf(t) for t in after_failed}
    gained = frozenset(t for t in names if t in bf and t in ap and t not in af)
    lost = frozenset(t for t in names if t in bp and t not in bf and t in af)
    red_both = frozenset(t for t in names if t in bf and t in af)
    earlier, later = compare_positions(
        positions_for(before_positions, red_both),
        positions_for(after_positions, red_both))
    moved = {m.key.test for m in (*earlier, *later)}
    same = {k.test for k, v in positions_for(after_positions, red_both).items()
            if before_positions.get(k) == v}
    return TestOutcomes(
        gained=gained, lost=lost, earlier=earlier, later=later,
        unpositioned=frozenset(red_both - moved - same))
