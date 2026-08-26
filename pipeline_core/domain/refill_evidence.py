"""Evidence behind a behavioural re-fill — what a prompt may CLAIM about a body
it asks the model to rewrite.

A [REFILL] mark says "this body compiles but its attributed test fails". The
frontier that stamps it is keyed on `(file, fn_name)`, and that key is weaker
than the claim in two ways the prompt used to hide:

* The test may sit in the DISPATCH projection only (a producer escalation or a
  traversal step — `FailureFrontier.convicting_for` is empty): the failure's
  path runs through a fn of this NAME, and nothing singles out this body. On
  2026-08-20 (run `902902-daee577d`) the frontier entry `src/context.rs::source`
  stamped BOTH `impl<C, E> StdError for ContextError<C, E>` (whose
  `Some(&self.error)` is correct) and `impl<C> StdError for
  ContextError<C, Error>` (the severed `None`); the prompt told the model the
  correct one "compiles but its test below FAILS — rewrite the BEHAVIOR" and
  showed the severed sibling as a settled `/* FILLED */` preview. The model's
  reasoning trace spent 71K characters concluding the target was right and the
  sibling was the bug, then hit the token ceiling.
* The same certainty rewrote `Error::construct_from_context` from an explicit
  per-type vtable (`object_downcast: context_downcast::<C, E>`, the shape the
  downcast tests need) into a `construct_from_std(ContextError { .. })`
  delegation, in the same run; the regression gate kept it because every
  attributed test was already red ("no self-failure is newly red"). A loop
  with no gradient on red tests walks downhill when it is told a correct body
  is wrong.

This module computes, per re-fill member, what the record actually supports:
the attributed tests; whether any of them CONVICTS this unit (DIRECT) or only
names it by inference (INFERRED); the same-named re-fill candidates in the
file's other impl blocks; and the failing tests' own words (panic site and
assertion, from `FailureFrontier.evidence`). Pure — no I/O, no prompt budget
decisions; the builders render what it returns.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum, auto

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, ImplBlock
from pipeline_core.domain.test_report import parse_panic_location


class Attribution(Enum):
    """How strongly the record implicates a re-fill target. Three states,
    because collapsing the last two into one bool is what locked the pass at
    92.

    DIRECT — an attributed test's OWN failure points at this unit. Unchanged
    meaning, unchanged prompt line.

    ESCALATED — a futility escalation added this unit AFTER measuring that
    rewriting its consumers changed no verdict. That is a MEASUREMENT, and it
    is the strongest evidence the loop ever produces about a producer: the
    accessors were tried and they did not pay.

    NAMED — a traversal step or a name match mentioned the fn and nothing was
    measured. Only this one deserves "leave it unchanged".

    Why the split. `direct` was a bool and `inferred` its complement, so
    ESCALATED and NAMED rendered identically — as `re-check (… implicated by
    NAME only — rewrite ONLY if it is wrong for its bounds, else leave it
    unchanged)`. On the live tree that grade went to the five
    `construct_from_*` in all three prompts that listed them, while `rewrite`
    went to `from` twenty times over — the accessors the escalation had just
    declared futile. The unit is escalated BECAUSE its consumers were measured
    futile, and was then handed the weakest instruction in the vocabulary.
    """

    DIRECT = auto()
    ESCALATED = auto()
    NAMED = auto()

@dataclass(frozen=True, slots=True)
class RefillEvidence:
    """One re-fill member and what the record says about it."""

    fn_name: str
    # Attributed failing tests (the dispatch projection), sorted, capped.
    tests: tuple[str, ...]
    # How the record implicates this unit (see :class:`Attribution`).
    attribution: Attribution
    # Headers of the other impl blocks in the same file whose same-named
    # member is also a re-fill target: the competing candidates.
    sibling_containers: tuple[str, ...]
    # (test, what it said) for the attributed tests that left a panic body.
    excerpts: tuple[tuple[str, str], ...]

    @property
    def direct(self) -> bool:
        """A test's own failure points here. Kept as the reading it always
        had — `not direct` is NO LONGER "inference only", because ESCALATED
        sits between the two; callers that meant that want
        :func:`inferred_refill_names`."""
        return self.attribution is Attribution.DIRECT


def container_header(container: str) -> str:
    """The impl header as one line, without the body brace."""
    head = container.split("{", 1)[0]
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    return " ".join(lines)


def quote_panic_site(
    text: str, read_source: Callable[[str], str | None], *, context: int = 2,
) -> str:
    """`text` (a failing test's own words, as `failure_evidence_by_test`
    keeps them) with the SOURCE LINE its panic names quoted beneath it.

    The evidence block used to carry the panic MESSAGE only — `panicked at
    tests/test_downcast.rs:17:56: called Result::unwrap() on an Err value:
    oh no!` — while the line at 17:56,
    `bail_literal().unwrap_err().downcast::<&str>().unwrap()`, is the
    observable itself, sitting in a test file the loop already holds. Run
    `263474-b0e608d3` batch 1 (2026-08-25): the constructor prompt carried
    the constructors first, the `#[repr(transparent)]` line, and this
    message; the model's trace read "test_downcast expects downcast for
    &str, String, io::Error … Should work." and re-emitted the delegating
    `construct_from_adhoc` verbatim. The message says a downcast failed; the
    line says WHICH payload must survive the round trip. `read_source`
    serves the crate's own trees (the reconstruction's `src/`, the oracle's
    `tests/`) and nothing else; a path it cannot serve, or a line past the
    file's end, leaves `text` unchanged."""
    file, line, _col, _msg = parse_panic_location(text)
    if not file or line is None:
        return text
    src = read_source(file)
    if not src:
        return text
    lines = src.splitlines()
    if not 1 <= line <= len(lines):
        return text
    lo, hi = max(1, line - context), min(len(lines), line + context)
    quoted = [f"{'>' if n == line else ' '} {n:>4} | {lines[n - 1]}"
              for n in range(lo, hi + 1)]
    return (text + f"\nat {file}:{line} — the line that panicked "
            "(the observable this test pins):\n" + "\n".join(quoted))


def collect_refill_evidence(
    block: ImplBlock,
    blocks: Iterable[ImplBlock],
    frontier: FailureFrontier | None,
    *,
    max_tests: int = 6,
    escalated: frozenset[FnLocator] = frozenset(),
    read_source: Callable[[str], str | None] | None = None,
) -> tuple[RefillEvidence, ...]:
    """What the frontier supports for each of `block.refill_members`.

    `blocks` is the file's (or cluster's) full block list as stamped by
    `collect_impl_refill_targets`, so a same-named member of another block in
    the same file that carries the same stamp is reported as a sibling
    candidate. With no frontier every member is INFERRED with no tests — the
    honest reading of a re-fill nobody can name a test for. With
    `read_source`, each excerpt carries the source line its panic names
    (`quote_panic_site`); without it the excerpt is the message alone."""
    out: list[RefillEvidence] = []
    for m in block.refill_members:
        key = FnLocator(block.file, m.fn_name)
        tests = (tuple(sorted(frontier.tests_for(key)))[:max_tests]
                 if frontier is not None else ())
        # A convicting test outranks the escalation: the unit's own failure is
        # better evidence than the measurement that its consumers were futile.
        if frontier is not None and frontier.convicting_for(key):
            grade = Attribution.DIRECT
        elif key in escalated:
            grade = Attribution.ESCALATED
        else:
            grade = Attribution.NAMED
        siblings = tuple(
            b.container for b in blocks
            if b.file == block.file and b.container != block.container
            and any(r.fn_name == m.fn_name for r in b.refill_members))
        excerpts: list[tuple[str, str]] = []
        if frontier is not None:
            for t in tests:
                text = frontier.evidence_for(t)
                if text and read_source is not None:
                    text = quote_panic_site(text, read_source)
                if text:
                    excerpts.append((t, text))
        out.append(RefillEvidence(
            fn_name=m.fn_name, tests=tests, attribution=grade,
            sibling_containers=siblings, excerpts=tuple(excerpts)))
    return tuple(out)


def inferred_refill_names(evidence: Iterable[RefillEvidence]) -> frozenset[str]:
    """Members the record NAMES and never measured — rendered `[REFILL?]` and
    graded `re-check`. Escalated members are deliberately NOT here: they were
    measured, and telling the model to leave them unchanged is what this split
    exists to stop."""
    return frozenset(e.fn_name for e in evidence
                     if e.attribution is Attribution.NAMED)


def escalated_refill_names(evidence: Iterable[RefillEvidence]) -> frozenset[str]:
    """Members a futility escalation added after measuring their consumers'
    rewrites as changing nothing."""
    return frozenset(e.fn_name for e in evidence
                     if e.attribution is Attribution.ESCALATED)


def attributed_test_leaves(
    evidence: Iterable[RefillEvidence], *, cap: int = 4,
) -> tuple[str, ...]:
    """The attributed tests' bare names, direct members first, deduplicated,
    capped — the lookup keys for putting the tests' SOURCE in the prompt. A
    printed `chain::test_rev` and the escalation's `test_rev` are the same
    test, so the last `::` segment is the key."""
    seen: list[str] = []
    for e in sorted(evidence, key=lambda e: not e.direct):
        for t in e.tests:
            leaf = t.rsplit("::", 1)[-1]
            if leaf and leaf not in seen:
                seen.append(leaf)
            if len(seen) >= cap:
                return tuple(seen)
    return tuple(seen)


def render_refill_evidence(
    evidence: Iterable[RefillEvidence], *, pins: str = "",
) -> str:
    """The REFILL section's body: per member, the strength of the claim, the
    attributed tests, and what they said. "" when there is nothing to say.

    `pins` is the oracle extraction of `pinned_observables` — the same
    instruction `_plan_line` carries for an ESCALATED write-list, so the
    per-impl route (which never calls `_plan_line`) still sees it."""
    lines: list[str] = []
    for e in evidence:
        if e.direct:
            lines.append(
                f"`{e.fn_name}` — DIRECT: a failing test's own failure points "
                f"at this body. Rewrite its BEHAVIOR; keep the signature.")
        elif e.attribution is Attribution.ESCALATED:
            # States the MEASUREMENT, never a certainty about this body. The
            # 2026-08-20 regression came from a prompt that ASSERTED a correct
            # body had failed, with no evidence attached; the model believed it
            # and replaced a working per-type vtable with a delegation. What is
            # true here is narrower and checkable: the fns that CONSUME this
            # value were rewritten and no verdict moved.
            lines.append(
                f"`{e.fn_name}` — ESCALATED: the fns that consume what this one "
                f"produces were rewritten over repeated sweeps and NO test "
                f"changed its verdict, so the defect is not in them — it is in "
                f"what this fn constructs or returns. This is a measurement, "
                f"not a claim about which line is wrong. Rewrite it.")
        else:
            lines.append(
                f"`{e.fn_name}` — INFERRED: a failing test's path runs through "
                f"a fn NAMED `{e.fn_name}` in this file; nothing singles out "
                f"THIS body. Rewrite it ONLY if it is wrong for its own bounds "
                f"and trait contract; if it is already right, return it "
                f"UNCHANGED — a different body written to look busy makes the "
                f"crate worse.")
            if e.sibling_containers:
                lines.append(
                    "  same-named re-fill candidate(s) in this file, tried "
                    "separately: " + ", ".join(
                        f"`{container_header(c)}`" for c in e.sibling_containers))
        if e.tests:
            lines.append("  attributed failing test(s): " + ", ".join(e.tests))
        for test, text in e.excerpts:
            lines.append(f"  {test} said:")
            lines.extend("    " + ln for ln in text.splitlines())
    if pins:
        lines.append(
            f"  the attributed tests pin: {pins}"
            + typeid_pin_consequence(pins))
    return "\n".join(lines)


_DOWNCAST_OR_IS_OPEN = re.compile(
    r"\b(downcast(?:_ref|_mut)?|is)\s*::\s*<")
_ASSERT_EQ_ALTDEBUG = re.compile(
    r'assert_eq!\s*\(\s*([^,]+?)\s*,\s*format!\s*\(\s*("{:#\?}")',
)
_FORMAT_ALT = re.compile(r'format!\s*\(\s*("{:#\?}")')


def _generic_inner(src: str, open_idx: int) -> str:
    """Inner of a `<…>` at `open_idx`, scanned by bracket depth.

    `[^>]+?` stops at the first `>`, so `is::<<str as ToOwned>::Owned>`
    truncated to `is::<<str as ToOwned>` — a form that is not Rust and
    is not what the test comment claimed the live glob returned."""
    if open_idx >= len(src) or src[open_idx] != "<":
        return ""
    depth = 0
    i = open_idx
    while i < len(src):
        c = src[i]
        if c == "<":
            depth += 1
        elif c == ">":
            if i > 0 and src[i - 1] in "-=":
                i += 1
                continue
            depth -= 1
            if depth == 0:
                return " ".join(src[open_idx + 1:i].split())
        i += 1
    return ""


def _item_start_including_attrs(src: str, fn_idx: int) -> int:
    """Start of the `#[test]` / attribute / comment lines immediately above
    `fn`, or of the fn's own line. Does not walk into the previous item."""
    line_start = src.rfind("\n", 0, fn_idx)
    fn_line_start = 0 if line_start < 0 else line_start + 1
    head = src[:fn_line_start]
    if not head:
        return fn_line_start
    lines = head.splitlines(keepends=True)
    take = 0
    for line in reversed(lines):
        stripped = line.strip()
        if (stripped.startswith("#[") or stripped.startswith("//")
                or stripped == ""):
            take += 1
            continue
        break
    if take == 0:
        return fn_line_start
    return len("".join(lines[: len(lines) - take]))


def test_item_source(file_src: str, leaf: str) -> str:
    """The `fn {leaf}` item from a test file, or "" if the name is absent.

    The needle is `fn {leaf}` followed by `(` or `<` so `fn test_downcast`
    does not take `fn test_downcast_ref` (`str.find("fn test_downcast")`
    is a prefix: glob-earlier `test_context.rs` answered with
    `test_downcast_ref`'s body — the 263257 leaf shape in
    `attributed_test_blocks`). Includes the `#[test]` / attribute lines
    immediately above the fn. Used so the write-list pin is taken from
    the attributed TEST, not from sibling tests in the same file
    (`test_drop`'s `downcast::<DetectDrop>` is not `test_downcast`'s
    TypeId pin)."""
    if not leaf or not file_src:
        return ""
    m = re.search(rf"\bfn\s+{re.escape(leaf)}\s*[<(]", file_src)
    if not m:
        return ""
    idx = m.start()
    start = _item_start_including_attrs(file_src, idx)
    rest = file_src[m.end():]
    nxt = rest.find("\nfn ")
    end = len(file_src) if nxt < 0 else m.end() + nxt
    return file_src[start:end]


def pinned_observables(*sources: str) -> str:
    """Oracle pins extracted from attributed test SOURCE.

    Run `2932-cf516410` (2026-08-26) quoted `test_downcast.rs` and the panic
    line `bail_literal().unwrap_err().downcast::<&str>()` in the constructor
    prompt's evidence block; the thinking named the TypeId wrap at trace
    line 45 and the reply still re-emitted `construct_from_std(MessageError(
    …))`. The missing slot was the INSTRUCTION (`_plan_line`), not another
    evidence channel. This quotes the test — `downcast::<T>`, `is::<T>`,
    `assert_eq!(EXPECTED_…, format!("{:#?}", …))` — never a crate-internal
    recipe (`divergences.md` forbids original internals on the write-list).

    `downcast_ref`/`downcast_mut` collapse to `downcast::<T>`: the TypeId
    the round-trip must preserve is the pin, not the method that reads it.
    Generic arguments are scanned by bracket depth so nested forms
    (`is::<<str as ToOwned>::Owned>`, `downcast::<Box<dyn StdError + Send
    + Sync>>`) survive; a `[^>]+?` regex stops at the first `>`.
    """
    pins: list[str] = []
    seen: set[str] = set()

    def _add(pin: str) -> None:
        pin = " ".join(pin.split())
        if pin and pin not in seen:
            seen.add(pin)
            pins.append(pin)

    for src in sources:
        if not src:
            continue
        for m in _DOWNCAST_OR_IS_OPEN.finditer(src):
            inner = _generic_inner(src, m.end() - 1)
            if not inner:
                continue
            method = m.group(1)
            if method.startswith("downcast"):
                _add(f"downcast::<{inner}>")
            else:
                _add(f"{method}::<{inner}>")
        for m in _ASSERT_EQ_ALTDEBUG.finditer(src):
            lhs = m.group(1).strip()
            _add(f'assert_eq!({lhs}, format!("{m.group(2)[1:-1]}", …))')
        if _FORMAT_ALT.search(src) and not any(
                'format!("{:#?}"' in p for p in pins):
            _add('format!("{:#?}", …)')
    return ", ".join(pins)


def pinned_observables_from_tests(
    leaves: Iterable[str], files: Iterable[str],
) -> str:
    """Pins of the attributed leaves.

    Whole-file fallback only when NO leaf produced a slice. An unsliceable
    leaf among successes is skipped (empty string) so a sibling's pins do
    not leak — per-leaf whole-file fallback is the poison the slice exists
    to prevent (`test_drop`'s `DetectDrop` into `test_downcast`'s prompt).
    """
    file_list = tuple(files)
    items: list[str] = []
    for leaf in leaves:
        found = ""
        for src in file_list:
            found = test_item_source(src, leaf)
            if found:
                break
        items.append(found or "")
    if any(items):
        return pinned_observables(*items)
    return pinned_observables(*file_list)


def typeid_pin_consequence(pins: str) -> str:
    """The TypeId consequence a `downcast::<T>` / `is::<T>` pin implies.

    Run `82157-476ddc16` listed those pins on the constructor `_plan_line`
    (`logs/prompts/49385be….txt`) and the reply still re-emitted a wrap
    before the generic constructor — a list of call forms is not the
    statement that wrapping the pinned type stores a different TypeId, so
    `downcast::<T>()` returns Err. Empty when `pins` has no TypeId
    observable (ALTDEBUG-only). Does not name a crate wrapper type.
    """
    if not pins:
        return ""
    if "downcast::<" not in pins and "is::<" not in pins:
        return ""
    return (" — wrapping the pinned type before the generic constructor "
            "stores a different TypeId, so those downcast::<T>() / is::<T>() "
            "calls return Err")
