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

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from pipeline_core.domain.abort_verdict import AbortPartition, located_tests
from pipeline_core.domain.crash_site import CrashSite
from pipeline_core.domain.generation import parse_gen_tests_filename
from pipeline_core.domain.test_results import (
    RegressionComparison,
    binary_key,
    binary_output_sections,
    world_sections,
)

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
    worlds = world_sections(capture or "")
    if "" not in worlds:
        # A capture that joined build worlds: each world's binaries are keyed
        # as `TestStateReading.keyed` keys them (`BinaryResult.key`).
        for w, text in worlds.items():
            for k, line in assertion_positions(text, source_of).items():
                out[TestKey(binary_key(w, k.binary), k.test)] = line
        return out
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
class SuiteDelta:
    """A joint write's own suite reading, as the abort partition reads it:
    the gain is a net pass change above zero, the loss is any test newly red
    or any binary newly crashed (`RegressionComparison`). The regression
    gate accepts a positive net beside a newly red test, so a positive net
    alone is not "nothing lost"; both halves come from this one reading.

    The file, impl and pair routes judged the gain on their aimed tests
    only, and a write deep in a chain gains tests no sweep attributed to it:
    run `4112620-8a89f05c` (2026-10-29) reverted `src/error.rs` file writes
    reading +4, +20 and +15 with nothing newly red or crashed, because the
    only aborts (already red, at a crate frame) were refused beside no
    aimed gain. The session already judged on the suite."""

    net: int
    lost: bool

    @classmethod
    def of(cls, cmp: RegressionComparison) -> SuiteDelta:
        return cls(net=cmp.net_pass_delta,
                   lost=bool(cmp.newly_red or cmp.newly_crashed))

    @property
    def gained(self) -> bool:
        return self.net > 0


@dataclass(frozen=True, slots=True)
class TestOutcomes:
    """Typed per-test outcomes of one attempt over its attributed tests.

    Each set is the names of tests judged per :class:`TestKey`
    (:func:`outcomes_of`). `gained`: not passing before, passing after.
    `lost`: passing before, failing after. `earlier` / `later`: failing in
    both, with a position under the same key in both, that moved. `unpositioned`: failing in both with no
    comparable position. `aborted`: ran to its own verdict before (passed,
    or failed with its own panic, in a process that did not abort), and
    after, its own process died without printing a panic — a native fault
    (stack exhaustion, a signal) in the code it now runs.

    :attr:`refuses` is the decided rule, and the only one: any loss refuses,
    an abort refuses by :class:`abort_verdict.AbortPartition` (a test that
    was green, or a red test whose crash names no crate frame, refuses
    whatever was gained; a red test whose crash names a crate frame is
    forgiven beside a strict gain and no loss), and an earlier position
    refuses only when nothing gained. `aborted_green` and `aborted_located`
    are set only by :func:`outcomes_of` from the crash sites it is handed;
    without sites every red abort is unlocated, so it refuses as an
    unlocated abort always has: it removes the measurement every later
    decision reads. Run `2157238-210637e6`
    (2026-10-11): a file route kept five `construct_from_*` bodies that each
    called themselves, red before and red after, "non-regression"; from the
    next sweep 26 test binaries died of stack exhaustion and 86 to 95 tests
    had no owner reading for the rest of the run. Every keep that
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
    aborted: frozenset[str] = frozenset()
    #: The aborted tests that passed before (a lost verdict).
    aborted_green: frozenset[str] = frozenset()
    #: The aborted tests that were failing before and whose crash site names
    #: an authorable crate frame (`abort_verdict.located_tests`).
    aborted_located: frozenset[str] = frozenset()

    @property
    def abort(self) -> AbortPartition:
        return AbortPartition.of(self.aborted, green_before=self.aborted_green,
                                 located=self.aborted_located)

    def refuses_with(self, *, strict_gain: bool) -> bool:
        """The decided rule with the caller's gain: a session judges on the
        suite's net, a route on the aimed tests it turned green, the ledger
        on what the landing's own keep turned green."""
        return (bool(self.lost)
                or self.abort.refuses(strict_gain=strict_gain,
                                      lost=bool(self.lost))
                or (bool(self.earlier) and not self.gained))

    @property
    def refuses(self) -> bool:
        return self.refuses_with(strict_gain=bool(self.gained))

    def refuses_on(self, suite: SuiteDelta) -> bool:
        """The decided rule for a joint write judged on its own suite
        reading: the aborts are partitioned with the suite's gain and the
        suite's loss beside the aimed set's; an aimed test lost still
        refuses, and the earlier-panic clause stays on the aimed set."""
        lost = bool(self.lost) or suite.lost
        return (lost
                or self.abort.refuses(strict_gain=suite.gained, lost=lost)
                or (bool(self.earlier) and not self.gained))

    @property
    def earlier_tests(self) -> tuple[str, ...]:
        return tuple(sorted({m.key.test for m in self.earlier}))

    @property
    def later_tests(self) -> tuple[str, ...]:
        return tuple(sorted({m.key.test for m in self.later}))


@dataclass(frozen=True, slots=True)
class KeyedVerdicts:
    """One reading's verdicts, each under its :class:`TestKey`.

    The only currency :func:`outcomes_of` accepts. A bare name is not a
    test: when two binaries each hold a test of one name, a name set holds
    that name in `passed` and in `failed` at once, and "passing before,
    failing after" is true of a test that was red before and after. Run
    `14495-a26ad4ea` (2026-10-15, cluster `stable`, fill 6):
    `tests/test_repr.rs::test_drop` passed and `tests/test_downcast.rs::
    test_drop` was red before and after a `src/error.rs` rewrite that read
    `pass 120 -> 127 … no regression`; the joint keep counted `test_drop`
    as newly red and reverted the rewrite on all three attempts.

    `passed` holds completed binaries only (a crashed binary's partial `ok`
    lines are a nondeterministic prefix); `failed`, `ignored`, `aborted` and
    `aborted_silent` hold every binary's."""

    __test__ = False

    passed: frozenset[TestKey] = frozenset()
    failed: frozenset[TestKey] = frozenset()
    ignored: frozenset[TestKey] = frozenset()
    aborted: frozenset[TestKey] = frozenset()
    aborted_silent: frozenset[TestKey] = frozenset()
    positions: Positions = field(default_factory=dict)

    @property
    def keys(self) -> frozenset[TestKey]:
        return self.passed | self.failed | self.ignored | self.aborted

    @classmethod
    def of_names(
        cls, *, passed: Iterable[str] = (), failed: Iterable[str] = (),
        ignored: Iterable[str] = (), aborted: Iterable[str] = (),
        aborted_silent: Iterable[str] = (), positions: Positions | None = None,
        binary: str = "",
    ) -> KeyedVerdicts:
        """A reading of ONE binary given by names: every name is a test of
        `binary`. For a view that never held more than one binary (an
        invented fixture); a multi-binary capture is read by its sections
        (`TestStateReading.keyed`), never through this."""
        def _k(names: Iterable[str]) -> frozenset[TestKey]:
            return frozenset(TestKey(binary, leaf(n)) for n in names)
        return cls(passed=_k(passed), failed=_k(failed), ignored=_k(ignored),
                   aborted=_k(aborted), aborted_silent=_k(aborted_silent),
                   positions=dict(positions or {}))


def outcomes_of(
    tests: Iterable[str], *, before: KeyedVerdicts, after: KeyedVerdicts,
    crash_sites: Sequence[CrashSite] = (),
) -> TestOutcomes:
    """The :class:`TestOutcomes` of `tests` (names; a qualified name is read
    by its leaf) between two readings, judged per :class:`TestKey`.

    Every key of either reading whose test is one of `tests` is judged on
    its own. `gained`: passing after (and not failing), and neither passing
    nor ignored before. `lost`: passing before (and not failing), failing
    after. `aborted`: ran to its own verdict before in a process that did
    not abort, and after its own process died without a panic (a death that
    printed a panic, such as a stub's `unimplemented!()` reached while
    unwinding, is not a native fault of the code under judgement).
    Positions compare under one key, over keys failing in both. The name
    sets on the result are the leaves of the judged keys: a name is lost
    when one of its tests was lost, never because another test of that name
    passes elsewhere."""
    names = {leaf(t) for t in tests}
    keys = {k for k in before.keys | after.keys if k.test in names}
    gained = {k for k in keys
              if k in after.passed and k not in after.failed
              and k not in before.passed and k not in before.ignored}
    lost = {k for k in keys
            if k in before.passed and k not in before.failed
            and k in after.failed}
    red_both = {k for k in keys if k in before.failed and k in after.failed}
    before_pos = {k: v for k, v in before.positions.items() if k in red_both}
    after_pos = {k: v for k, v in after.positions.items() if k in red_both}
    earlier, later = compare_positions(before_pos, after_pos)
    moved = {m.key for m in (*earlier, *later)}
    same = {k for k, v in after_pos.items() if before_pos.get(k) == v}
    aborted = {k for k in keys
               if (k in before.passed or k in before.failed)
               and k not in before.aborted and k in after.aborted_silent}
    # The abort partition's inputs (`abort_verdict.AbortPartition`): which
    # aborted tests passed before, and which red ones a crash site of
    # `crash_sites` locates in the crate. No sites, nothing located.
    aborted_green = {k for k in aborted
                     if k in before.passed and k not in before.failed}
    located = located_tests(crash_sites) if crash_sites else frozenset()
    return TestOutcomes(
        gained=frozenset(k.test for k in gained),
        lost=frozenset(k.test for k in lost),
        earlier=earlier, later=later,
        unpositioned=frozenset(k.test for k in red_both - moved - same),
        aborted=frozenset(k.test for k in aborted),
        aborted_green=frozenset(k.test for k in aborted_green),
        aborted_located=frozenset(k.test for k in aborted - aborted_green
                                  if k.test in located))
