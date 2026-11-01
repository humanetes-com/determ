"""Behaviour probes — pure domain half of the synthesis tier.

A PROBE is a model-authored program that USES the crate's public API and
PRINTS an observable; the harness runs it against the ORIGINAL crate in the
pre-strip window and records stdout as the behaviour's Then. The model never
guesses the Then — it only chooses what to observe — so a probe that compiles
and runs is a golden reading, and one that panics or fails to build is
discarded (self-filtering, never a hallucinated oracle).

This module owns what needs no cargo: the result record, the reply parser for
the `<BEHAVIOUR unit="…">` blocks the synthesis prompt asks for, stdout
normalisation, the target selection (public fns no harvested spec names) and
the spec constructor. The adapter (`adapters/behaviour_probe.py`) writes the
example file and runs it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

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
    example_code,
    has_resource_boundary,
    is_probe_id,
)
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.fn_reachability import PublicIndex
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.rust_idents import impl_self_type
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.rust_scan import contains_word
from pipeline_core.domain.text import strip_think_blocks

_OPEN_TAG = "<BEHAVIOUR"
_CLOSE_TAG = "</BEHAVIOUR>"
_FENCE = "```"
# Consumers of the public API, never part of it — a probe target is a crate fn.
_AUX_DIRS = frozenset({"tests", "examples", "benches"})
PROBE_BATCH_SIZE = 6


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe program's run against the original crate."""

    ok: bool
    stdout: str
    stderr: str


class ProbeBuild(Enum):
    """How the original crate is built for one run of a probe program.

    AS_BUILT is the crate's own profile. WITHOUT_SYMBOLS is the same source
    with no symbol table and no debug info in the binary, so nothing a
    runtime prints by symbolizing the running binary (function names, source
    paths, lines and columns of a captured backtrace) survives. A render's
    agreement across the two builds is what the crate prints whatever its
    build; the rest is what the build owns, and a reconstruction with other
    private fns or other line numbers renders it differently. Measured
    2026-10-31 on an invented crate: a probe binary and a test binary agree
    on a captured backtrace through the crate's own frames at their line and
    column (`tests/integration/test_generated_test_worlds.py`); the
    no-symbols build agrees with the as-built one up to the first frame's
    number."""

    AS_BUILT = "as_built"
    WITHOUT_SYMBOLS = "without_symbols"


def normalize_stdout(text: str) -> str:
    """Trailing whitespace stripped per line, trailing blank lines dropped —
    the form a Then is stored in and compared against."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def printed_value(stdout: str) -> str:
    """What a `println!`-of-one-expression capture OBSERVED, exactly.

    NOT :func:`normalize_stdout`. That one strips trailing whitespace PER LINE
    and drops trailing blank lines, which is right for a free-form program's
    multi-line blob and wrong for a single rendering: the replay test asserts
    `format!(…) == <stored value>`, so any character the store drops that
    `format!` still produces makes the emitted test fail against the ORIGINAL
    crate. Measured 2026-08-27 on a pristine copy: a value of `"a \nb"` printed
    as `'a \nb\n'`, stored by `normalize_stdout` as `'a\nb'`, and the emitted
    `#[test]` failed on the very tree the value was captured from. `println!`
    appends exactly one newline, so removing exactly one is the inverse."""
    if stdout.endswith("\r\n"):
        return stdout[:-2]
    if stdout.endswith("\n"):
        return stdout[:-1]
    return stdout


def _strip_code_fence(code: str) -> str:
    """The body of a ```rust … ``` fence when the block is wrapped in one,
    else the text unchanged. Models fence the program despite the tag."""
    s = code.strip()
    if not s.startswith(_FENCE):
        return s
    nl = s.find("\n")
    if nl < 0:
        return ""
    body = s[nl + 1 :]
    end = body.rfind(_FENCE)
    if end >= 0:
        body = body[:end]
    return body.strip()


def _attr(tag: str, key: str) -> str:
    """The `key="…"` (or single-quoted) value of an opening tag, or ""."""
    at = tag.find(key)
    if at < 0:
        return ""
    eq = tag.find("=", at)
    if eq < 0:
        return ""
    i = eq + 1
    while i < len(tag) and tag[i].isspace():
        i += 1
    if i >= len(tag) or tag[i] not in "\"'":
        return ""
    q = tag[i]
    end = tag.find(q, i + 1)
    return "" if end < 0 else tag[i + 1 : end].strip()


def _unit_attr(tag: str) -> str:
    """Back-compat alias: the `unit=` attribute of an opening tag."""
    return _attr(tag, "unit")


def parse_behaviour_programs(reply: str) -> list[tuple[str, str]]:
    """`(unit_id, program)` for every `<BEHAVIOUR unit="…">…</BEHAVIOUR>` block
    in a synthesis reply, in order; think blocks and a wrapping code fence
    are stripped; empty programs are dropped. A block with no unit attribute
    keeps an empty unit id (the caller labels it by batch)."""
    text = strip_think_blocks(reply)
    out: list[tuple[str, str]] = []
    i = 0
    while (at := text.find(_OPEN_TAG, i)) >= 0:
        gt = text.find(">", at)
        if gt < 0:
            break
        close = text.find(_CLOSE_TAG, gt + 1)
        if close < 0:
            break
        unit = _unit_attr(text[at:gt])
        program = _strip_code_fence(text[gt + 1 : close])
        if program:
            out.append((unit, program))
        i = close + len(_CLOSE_TAG)
    return out


# --- the captured-behaviour shape (setup + ONE expression + a render kind) ---
#
# Measured 2026-08-27 on this model and this crate: the free-form `fn main()`
# the old prompt asked for produced 0 of 6 mechanically-assertable observables
# (3 recorded on disk, 3 re-run as the experiment's control), every one a
# multi-`println!` blob stored as NORMALIZED_STDOUT and classified OTHER — so
# `behaviours_pinned_by` (which joins on an exact `then.value`) could never
# match one, and t1.5's cap discarded them for ranking last. The constrained
# shape produced 7 of 7 across two variants. `when` is therefore ONE
# expression, and the harness renders BOTH roles from it: `println!` to
# capture, `assert_eq!` to replay.

_OBSERVE_TO_KIND: Mapping[str, OutcomeKind] = {
    "display": OutcomeKind.DISPLAY_STRING,
    "debug": OutcomeKind.DEBUG_STRING,
    "alt_debug": OutcomeKind.ALT_DEBUG_STRING,
}
_KIND_TO_FMT: Mapping[OutcomeKind, str] = {
    OutcomeKind.DISPLAY_STRING: "{}",
    OutcomeKind.DEBUG_STRING: "{:?}",
    OutcomeKind.ALT_DEBUG_STRING: "{:#?}",
}
# A `when` that COMPARES discards the value it was supposed to specify:
# variant B of the experiment produced `format!("{}", b) == "world"` whose
# captured Then is `true`, carrying nothing. Identity predicates are the one
# exception, and the same carve-out `weak_assertion_reason` already makes for
# generated tests (`behaviour_pinning.py`, `_IDENTITY_METHODS`).
_COMPARISON_TOKENS = ("==", "!=", "<=", ">=", ".contains(")
# `&&` / `||` are refused for the same reason a comparison is, one step further
# on: `a.is::<X>() && b.is::<Y>()` prints one boolean, and the capture would
# store it as the identity of X alone — a pin that says something the run never
# measured. A conjunction is two behaviours; ask for them separately.
_LOGIC_TOKENS = ("&&", "||")
_IDENTITY_CALLS = (".is::<", ".downcast_ref::<", ".downcast_mut::<", ".downcast::<")


@dataclass(frozen=True, slots=True)
class CapturedProbe:
    """One `<BEHAVIOUR>` block of a synthesis reply, in the constrained shape."""

    name: str
    given: str
    when: str
    observe: str


_IDENTITY_METHOD_NAMES = frozenset(c[1:-3] for c in _IDENTITY_CALLS)
# A presence test over an identity call is still that identity
# (`e.downcast_ref::<T>().is_some()` prints whether the type held).
_IDENTITY_PRESENCE = frozenset({"is_some", "is_ok"})
_OPTION_PRESENCE = frozenset({"is_some", "is_none"})
# Characters that, at the top level of the text in front of a call, make an
# operator (not the call) the root of the value.
_ROOT_OPERATOR_CHARS = frozenset("=!<>+-*/%^|&")


@dataclass(frozen=True, slots=True)
class RootCall:
    """The outermost call of a when's value (:func:`value_root`), literals
    blanked: ``receiver.name::<turbofish>(…)``, ``path::name(…)`` or
    ``name!(…)``. ``receiver`` is "" unless ``method``; ``turbofish`` is the
    call's own type argument, "" when it has none."""

    receiver: str
    name: str
    turbofish: str
    method: bool
    macro: bool


def _open_before(s: str, close: int) -> int | None:
    """Index of the bracket opening the one that closes at ``close``."""
    depth = 0
    for i in range(close, -1, -1):
        if s[i] in ")]}":
            depth += 1
        elif s[i] in _OPEN:
            depth -= 1
            if depth == 0:
                return i
    return None


def _turbofish_open(callee: str) -> int | None:
    """Index of the `<` matching the `>` that ends ``callee`` (a `->` inside
    the type is not a bracket)."""
    depth = 0
    for i in range(len(callee) - 1, -1, -1):
        c = callee[i]
        if c == ">" and not (i > 0 and callee[i - 1] == "-"):
            depth += 1
        elif c == "<":
            depth -= 1
            if depth == 0:
                return i
    return None


def root_call(when: str) -> RootCall | None:
    """The call whose value IS the when's value, or None when the root is not
    a call (an operator, a literal, a field, a closure).

    Every decision about what a when observes reads this, not a token found
    anywhere in the text. A token scan classified
    `format!("{:?}", err.downcast_mut::<String>())` as a type identity because
    a downcast sat inside the format's argument; the replay then asserted a
    String as a bool, the emitted file did not compile, and every capture in
    it was withdrawn (run `15370-6a4cfd45`, 2026-10-30: 24 stable and 26
    nightly captures; run `2129106-ead996ab`, 52)."""
    s = value_root(when).strip()
    if not s.endswith(")"):
        return None
    open_ = _open_before(s, len(s) - 1)
    if open_ is None or s[open_] != "(":
        return None
    callee = s[:open_].rstrip()
    turbofish = ""
    if callee.endswith(">"):
        lt = _turbofish_open(callee)
        if lt is None or not callee[:lt].endswith("::"):
            return None
        turbofish = callee[lt + 1:-1].strip()
        callee = callee[:lt - 2].rstrip()
    macro = callee.endswith("!")
    if macro:
        callee = callee[:-1].rstrip()
    j = len(callee)
    while j > 0 and (callee[j - 1].isalnum() or callee[j - 1] == "_"):
        j -= 1
    name = callee[j:]
    if not name:
        return None
    before = callee[:j].rstrip()
    # The call is the root only when nothing in front of it is an operator
    # of the root: `a == Some(3)` ends in a call and is a comparison,
    # `!e.is::<T>()` is a negation, `&x.f()` a reference.
    top = _top_level_text(without_turbofish(before))
    if any(c in top for c in _ROOT_OPERATOR_CHARS) or " as " in f" {top} ":
        return None
    method = not macro and before.endswith(".")
    receiver = before[:-1].rstrip() if method else ""
    return RootCall(receiver=receiver, name=name, turbofish=turbofish,
                    method=method, macro=macro)


def identity_type_of(when: str) -> str | None:
    """The `T` of an identity predicate, else None: the when's root call is
    `x.is::<T>()` / `x.downcast_ref::<T>()` / `downcast_mut` / `downcast`,
    or `.is_some()` / `.is_ok()` over such a call. `TYPE_IDENTITY` stores T
    as its value — `DOWNCAST_OK` carries a bool and NOT which type, which is
    why it was retired on 2026-08-26.

    Only the root decides (:func:`root_call`): a downcast inside a format, a
    comparison or a combinator's closure is an argument of the value, not
    the value, and its when observes something else."""
    rc = presence_target(root_call(when))
    if rc is None or not rc.method or rc.name not in _IDENTITY_METHOD_NAMES:
        return None
    return rc.turbofish or None


def presence_target(rc: RootCall | None) -> RootCall | None:
    """``rc``, or for `.is_some()` / `.is_ok()` the root call of its receiver
    (repeated), so an identity read through a presence test is the identity
    call itself."""
    while (rc is not None and rc.method and not rc.turbofish
           and rc.name in _IDENTITY_PRESENCE and rc.receiver):
        inner = root_call(rc.receiver)
        if inner is None:
            return rc
        rc = inner
    return rc


def observes_presence(when: str) -> bool:
    """True when the when's value is an `Option`'s presence: its root call is
    `.is_some()` or `.is_none()`. Read at the root for the same reason as
    :func:`identity_type_of`."""
    rc = root_call(when)
    return rc is not None and rc.method and rc.name in _OPTION_PRESENCE


def without_turbofish(when: str) -> str:
    """`when` with every `::<…>` span removed, angle brackets included.

    The bare `<` and `>` of a comparison and the `<` and `>` of a turbofish are
    the same character, which is why the first version of this check omitted
    them and admitted `v.len() < 3` and `n > 0` — both of which observe a
    boolean and discard the value, exactly what the check exists to refuse.
    Deleting the turbofish spans first makes the remaining angle brackets
    unambiguous."""
    out: list[str] = []
    i = 0
    while i < len(when):
        if when.startswith("::<", i):
            depth = 0
            j = i + 2
            while j < len(when):
                if when[j] == "<":
                    depth += 1
                elif when[j] == ">" and when[j - 1] != "-":  # `->` is not a bracket
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(when):  # unbalanced: keep the tail verbatim
                out.append(when[i:])
                break
            i = j + 1
            continue
        out.append(when[i])
        i += 1
    return "".join(out)


_OPEN = {"(": ")", "[": "]", "{": "}"}
# A `|` or `<` in operand position (nothing that could be a left operand in
# front of it) opens a closure's parameter list or a qualified path; after an
# operand it is a binary operator.
_OPERAND_END = frozenset("([{,=;!&|+-*/%^<>:")


def _blank_literals(expr: str) -> str:
    """``expr`` with every string, char literal and comment blanked to spaces
    (`rust_lex.code_mask`), positions kept, so brackets and operators inside
    a literal are not read as code."""
    mask = code_mask(expr)
    return "".join(" " if m else c for c, m in zip(expr, mask, strict=True))


def _close_of(s: str, at: int) -> int | None:
    """Index of the bracket closing the one opened at ``at`` (any of `([{`)."""
    depth = 0
    for i in range(at, len(s)):
        if s[i] in _OPEN:
            depth += 1
        elif s[i] in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _operand_before(s: str, i: int) -> bool:
    prev = s[:i].rstrip()
    return bool(prev) and prev[-1] not in _OPERAND_END and not prev.endswith("move")


def _closure_body(inner: str) -> str | None:
    """The body of a closure literal ``inner`` (`|x| e`, `move || { … }`,
    `|| -> T { … }`), or None when ``inner`` is not one."""
    s = inner.strip()
    if s.startswith("move"):
        s = s[4:].lstrip()
    if not s.startswith("|"):
        return None
    if s.startswith("||"):
        rest = s[2:]
    else:
        end = s.find("|", 1)
        if end < 0:
            return None
        rest = s[end + 1:]
    rest = rest.strip()
    if rest.startswith("->"):
        brace = rest.find("{")
        if brace < 0:
            return None
        rest = rest[brace:]
    return rest


def _block_tail(block: str) -> str:
    """The tail expression of ``{ … }`` (after the last statement), "" for a
    block whose value is `()`."""
    body = block[1:-1]
    depth, last = 0, -1
    for i, c in enumerate(body):
        if c in _OPEN:
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            last = i
    return body[last + 1:].strip()


def value_root(when: str) -> str:
    """The sub-expression of ``when`` whose value IS the when's value, with
    literals blanked.

    Grouping parentheses are peeled. A block's value is its tail expression,
    and an `unsafe` block's too. A call of a parenthesised closure literal is
    that closure's body, the body's tail when it is a block. Anything else is
    its own value: a call or a method call is the call (an operator inside an
    argument or inside a closure that is passed, not called, is not the
    value), and a closure that is not called is the closure."""
    s = _blank_literals(when).strip()
    for _ in range(64):
        if s.startswith("unsafe") and s[6:].lstrip().startswith("{"):
            s = s[6:].lstrip()
        if not s:
            return s
        if s[0] in "({":
            close = _close_of(s, 0)
            if close is None:
                return s
            rest = s[close + 1:].strip()
            if not rest:
                if s[0] == "{":
                    s = _block_tail(s)
                    continue
                inner = s[1:-1]
                if not _has_top_level(inner, ","):
                    s = inner.strip()
                    continue
                return s
            if s[0] == "(" and rest.startswith("(") and _close_of(rest, 0) == len(rest) - 1:
                callee = s[1:close].strip()
                # Extra grouping around the callee (`((|x| e))(x)`) is still
                # the closure. A top-level comma is a tuple, not a group.
                for _peel in range(8):
                    if not callee.startswith("("):
                        break
                    inner_close = _close_of(callee, 0)
                    if inner_close is None or inner_close != len(callee) - 1:
                        break
                    inner = callee[1:-1].strip()
                    if _has_top_level(inner, ","):
                        break
                    callee = inner
                body = _closure_body(callee)
                if body is not None:
                    s = body
                    continue
        return s
    return s


def _has_top_level(s: str, ch: str) -> bool:
    depth = 0
    for c in s:
        if c in _OPEN:
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ch and depth == 0:
            return True
    return False


def _top_level_text(root: str) -> str:
    """``root`` with every bracketed group, every closure (from its parameter
    list on) and every qualified path `<T as Trait>` blanked: the operators
    left are the root's own."""
    out: list[str] = []
    depth = 0
    i, n = 0, len(root)
    while i < n:
        c = root[i]
        if c in _OPEN:
            depth += 1
            out.append(c if depth == 1 else " ")
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            out.append(c if depth == 0 else " ")
            i += 1
            continue
        if depth == 0 and c == "<" and not _operand_before(root, i):
            d, k = 0, i
            while k < n:
                if root[k] == "<":
                    d += 1
                elif root[k] == ">":
                    d -= 1
                    if d == 0:
                        break
                k += 1
            out.append(" " * (min(k, n - 1) + 1 - i))
            i = k + 1
            continue
        if depth == 0 and c == "|":
            if not _operand_before(root, i):
                out.append(" " * (n - i))
                break
            if root.startswith("||", i):
                out.append("||")
                i += 2
                continue
        out.append(c if depth == 0 else " ")
        i += 1
    return "".join(out)


def value_bearing_reason(when: str) -> str | None:
    """Why this `when` may NOT be captured, or None when it may.

    A comparison observes `true`/`false` and throws the value away — the same
    weakness `weak_assertion_reason` refuses on the generation side, arriving
    on the capture side. Identity predicates are exempt: for them the boolean
    IS the observable, and the type is kept in the Then's value.

    The operator judged is the one at the root of the value (:func:`value_root`),
    not one anywhere in the text. A substring scan refused every when that
    compares inside a macro argument, a call argument or a closure it then
    unwraps (`(|| -> Result<()> { ensure!(a >= b); Ok(()) })().unwrap_err()`,
    whose value is the error): 110 of the 634 whens task 0d proposed on the
    record (2026-10-04 → 10-27), every one of them a false refusal, 16 of 25
    on run `2355041-eae39441` (`scripts/value_bearing_replay.py`)."""
    w = (when or "").strip()
    if not w:
        return "empty when"
    if "\n" in w:
        return "when spans multiple lines — it must be ONE expression"
    if "println!" in w or "print!" in w:
        return "when prints; the harness prints it"
    if "assert" in w:
        return "when asserts; the harness records, it does not check"
    top = _top_level_text(without_turbofish(value_root(w)))
    for tok in _LOGIC_TOKENS:
        if tok in top:
            return (
                f"when combines two observables ({tok}) — one printed "
                "boolean cannot say which half held; write them separately"
            )
    if identity_type_of(w) is not None:
        return None
    for tok in (*_COMPARISON_TOKENS, "<", ">"):
        if tok in top:
            return (
                f"when compares ({tok}) — a boolean discards the value; "
                "write the expression whose value is the observable"
            )
    return None


def observe_kind(observe: str, when: str = "") -> OutcomeKind:
    """The Then kind this capture stores. An identity `when` is TYPE_IDENTITY
    whatever the model said it would observe — the shape of the expression is
    the authority, not the label."""
    if identity_type_of(when) is not None:
        return OutcomeKind.TYPE_IDENTITY
    return _OBSERVE_TO_KIND.get((observe or "").strip().lower(), OutcomeKind.DISPLAY_STRING)


def other_observes(probe: CapturedProbe) -> tuple[str, ...]:
    """The renderings `probe` was NOT asked under, in a fixed order: the
    further observations one program can yield. None for an identity
    predicate (a boolean has one rendering that means anything)."""
    if identity_type_of(probe.when) is not None:
        return ()
    asked = observe_kind(probe.observe, probe.when)
    return tuple(o for o in ("display", "debug", "alt_debug") if observe_kind(o, probe.when) is not asked)


def render_probe_program(
    given: str,
    when: str,
    observe: str = "display",
    *,
    crate: str = "",
) -> str:
    """The CAPTURE role: a runnable `fn main()` that PRINTS the observable.

    `crate` puts `use <crate>::*;` above `fn main()`, and omitting it is not a
    detail. The three probes this shape replaced each opened with
    `use anyhow::{anyhow, Error};` OUTSIDE `fn main()` — the one line the
    constrained shape has nowhere to put, since `given` is spliced INSIDE the
    body. Measured 2026-08-27 against a pristine copy of the original: a
    `given` of `let err = anyhow!("boom");` with no import fails to build with
    `error: cannot find macro `anyhow` in this scope`, so the capture is lost
    and only a miss records it. The replay half has had this since it was
    written (`as_integration_test` inserts the same line); this makes the two
    roles symmetric. A `use` the model puts in `given` anyway still shadows the
    glob and still compiles.

    Also what `exercises_by_coverage` re-runs — before this existed it re-ran
    the stored setup directly, so a setup that stopped being a whole program
    would have made every probe's exercises run fail `rc != 0` and be dropped
    with a log line."""
    kind = observe_kind(observe, when)
    body = (given or "").rstrip()
    if kind is OutcomeKind.TYPE_IDENTITY:
        expr = f'println!("{{}}", {when.strip()});'
    else:
        expr = f'println!("{_KIND_TO_FMT[kind]}", {when.strip()});'
    head = f"#[allow(unused_imports)]\nuse {crate}::*;\n\n" if crate else ""
    return head + "fn main() {\n" + (body + "\n" if body else "") + "    " + expr + "\n}\n"


def parse_behaviour_specs(reply: str) -> list[CapturedProbe]:
    """Every `<BEHAVIOUR name="…"><GIVEN>…</GIVEN><WHEN>…</WHEN>
    <OBSERVE>…</OBSERVE></BEHAVIOUR>` block, in order.

    A block without a `<WHEN>` is dropped: the expression IS the behaviour.
    The class is NOT read even when present — measured 0 of 3 correct when the
    model chose it against 4 of 4 when it was supplied, and `classified()`
    re-derives it from kind and text anyway."""
    text = strip_think_blocks(reply)
    out: list[CapturedProbe] = []
    i = 0
    while (at := text.find(_OPEN_TAG, i)) >= 0:
        gt = text.find(">", at)
        if gt < 0:
            break
        close = text.find(_CLOSE_TAG, gt + 1)
        if close < 0:
            break
        block = text[gt + 1 : close]
        when = _tagged(block, "WHEN")
        if when:
            out.append(
                CapturedProbe(
                    name=_attr(text[at:gt], "name"),
                    given=_strip_code_fence(_tagged(block, "GIVEN")),
                    when=_strip_code_fence(when).strip(),
                    observe=_tagged(block, "OBSERVE").strip() or "display",
                )
            )
        i = close + len(_CLOSE_TAG)
    return out


def _tagged(block: str, tag: str) -> str:
    """The body of `<TAG>…</TAG>` inside one behaviour block, or ""."""
    o, c = f"<{tag}>", f"</{tag}>"
    a = block.find(o)
    if a < 0:
        return ""
    b = block.find(c, a + len(o))
    return block[a + len(o) : b] if b >= 0 else ""


# The environment variables that change what a probe OBSERVES rather than how
# it builds. `RUST_BACKTRACE` is the measured one: `err.backtrace()` renders
# `<disabled>` without it and a captured backtrace with it, identically on
# every run either way, so the run-twice guard cannot see the difference.
OBSERVABLE_ENV_KEYS = ("RUST_BACKTRACE", "RUST_LIB_BACKTRACE")


def perturbed_env(env: Mapping[str, str]) -> dict[str, str]:
    """The env flip that EXPOSES an environment-dependent observable.

    Run-twice cannot see this axis: measured 2026-08-27 against a pristine
    copy, `err.backtrace()` renders `"disabled backtrace"` on every run at a
    fixed env and a captured backtrace under `RUST_BACKTRACE=1`. A capture
    that changes across this flip is TWO specifications of the crate — one
    per world — not zero. The flip is how the other world is found; each
    world is recaptured, stored with `required_env`, and emitted as its own
    test. A previous capture_env field on the spec was write-only and caught
    nothing."""
    out: dict[str, str] = {}
    for key in OBSERVABLE_ENV_KEYS:
        current = (env.get(key) or "").strip().lower()
        out[key] = "0" if current in ("1", "full") else "1"
    return out


def unperturbed_env(perturb: Mapping[str, str]) -> dict[str, str]:
    """The world the first capture ran in, given the flip that exposes the other.

    `perturbed_env({})` is `{RUST_BACKTRACE: 1, …}`; the capture world is the
    inverse (`0`)."""
    out: dict[str, str] = {}
    for key, val in perturb.items():
        vv = (val or "").strip().lower()
        out[key] = "0" if vv in ("1", "full") else "1"
    return out


def env_pairs(env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in env.items() if k))


def env_twin_id(behaviour_id: str) -> str:
    """A second world of the same probe. Still a `probe::` id so replayable()
    keeps it; a suffix rather than a new n so the capture-world id is stable."""
    return behaviour_id if behaviour_id.endswith("::env") else f"{behaviour_id}::env"


def outcome_from_stdout(spec: BehaviourSpec, stdout: str) -> ObservableOutcome:
    """Re-read a probe's Then from a later run's stdout, keeping the original
    kind (and, for identity, the original type name)."""
    text = printed_value(stdout)
    then = spec.then
    if then is None:
        return ObservableOutcome.display(text)
    kind = then.kind
    if kind is OutcomeKind.TYPE_IDENTITY:
        return ObservableOutcome.type_identity(then.value, ok=text.strip() == "true")
    if kind is OutcomeKind.DEBUG_STRING:
        return ObservableOutcome.debug(text)
    if kind is OutcomeKind.ALT_DEBUG_STRING:
        return ObservableOutcome.alt_debug(text)
    return ObservableOutcome.display(text)


def capture_moved(spec: BehaviourSpec, stdout: str) -> bool:
    """True when `stdout` is a different observable than spec.then — the
    recapture found another env world, not a flaky re-print of the same one."""
    new = outcome_from_stdout(spec, stdout)
    old = spec.then
    if old is None:
        return True
    if old.kind is OutcomeKind.TYPE_IDENTITY:
        return bool(new.ok) != bool(old.ok)
    return new.value != old.value


#: The replay prints what it actually rendered when a world-pinned assertion
#: fails, so the emitter can read it out of the cargo output. Ours, not
#: rustc's `assertion left == right` layout, which is a format we do not own.
OBSERVED_OPEN = "determ-observed["
OBSERVED_MID = "]<<<"
OBSERVED_CLOSE = ">>>"
_OBSERVED_RE = re.compile(
    re.escape(OBSERVED_OPEN) + r"(?P<fn>[A-Za-z0-9_]+)"
    + re.escape(OBSERVED_MID) + r"(?P<value>.*?)" + re.escape(OBSERVED_CLOSE),
    re.DOTALL)


def observed_values(output: str) -> dict[str, str]:
    """`replay fn name -> the text it rendered`, from a failed run's output.

    Empty when nothing failed with a marker — an older emitted file, or a
    failure that is not an assertion of ours."""
    return {m["fn"]: m["value"] for m in _OBSERVED_RE.finditer(output)}


def narrow_then_to_agreement(
    spec: BehaviourSpec, observed: str,
) -> BehaviourSpec | None:
    """`spec` with its Then cut back to the text it and `observed` share, or
    `None` when they share nothing (the caller withdraws).

    THE PAIR THAT MATTERS IS CAPTURE-vs-REPLAY. The capture runs in the probe
    binary and the replay in the test binary, so any part of a render that
    names the rendering PROCESS differs between them — and two runs of the
    same probe example agree on exactly that part, which is why comparing
    probe runs to each other cannot find it. Run `330242-0be2c3e1`
    (2026-09-03) emitted the twin as the probe's whole render and the next
    line of both clusters was `1 capture(s) did not reproduce as a test
    against the original and were withdrawn`. Narrowing keeps the crate's own
    output (the message, the chain, the header that says a tail follows) and
    drops the bytes the process contributed. Strictly shortening, so the
    emitter's re-render loop terminates."""
    then = spec.then
    if then is None or not then.value:
        return None
    shared = agreed_prefix([then.value, observed])
    if not shared or shared == then.value:
        return None
    return replace(spec, then=replace(then, value=shared, prefix=True))


def agreed_prefix(values: Sequence[str]) -> str:
    """The longest leading text every value shares that ends where every
    value is at a token boundary. `""` for fewer than two values, or none.
    Pure.

    A token is a run of non-whitespace characters. The shared characters of
    two DIFFERENT tokens are not an agreement. They are where the two
    renders part, and they pin the first characters of one side. Run
    `3178980-c42f7944` (2026-11-01): the stripped build rendered the first
    frame `<unknown>`, and the symbolized build rendered a `<T as
    Trait>::method` frame. The character prefix kept their shared `<`, so 4
    twins pinned `   0: <`. Since std drops its own capture frames, that
    `<` says the original captures inside a trait-impl method, and a
    reconstruction that captures from an inherent or a free fn is red on
    those tests forever. The cut goes back to the last point where no value
    continues a token, here `   0: `. A value that ends there is at a
    boundary. Both writers of a prefix Then read this function
    (:func:`split_env_worlds` and :func:`narrow_then_to_agreement`), so
    neither can pin part of a token. It names no format, no runtime and no
    placeholder.

    An agreement of whitespace alone is `""`. Two renders that share only
    their indentation (`"  A(1)"` and `"  A(2)"` agree on `"  "` once the
    cut leaves the token) agree on no output of the crate, and a prefix Then
    of blanks plus a longer render passes almost any reconstruction that
    prints anything: a test that cannot go red is not an observable, so the
    caller withdraws as it does when nothing is shared."""
    if len(values) < 2:
        return ""
    head = values[0]
    for other in values[1:]:
        i = 0
        limit = min(len(head), len(other))
        while i < limit and head[i] == other[i]:
            i += 1
        head = head[:i]
        if not head:
            return ""
    i = len(head)
    while i > 0 and not all(_token_boundary(v, i) for v in values):
        i -= 1
    cut = head[:i]
    return cut if cut.strip() else ""


def _token_boundary(value: str, i: int) -> bool:
    """True when position `i` of `value` is not inside a run of
    non-whitespace characters."""
    return (i <= 0 or i >= len(value)
            or value[i - 1].isspace() or value[i].isspace())


def split_env_worlds(
    spec: BehaviourSpec,
    perturb_stdout: str | Sequence[str],
    perturb_env: Mapping[str, str],
) -> tuple[BehaviourSpec, BehaviourSpec] | None:
    """`(capture-world spec, perturb-world twin)` from recaptures whose Then
    moved. The original keeps its id and Then; the twin is `::env`.

    THE TWIN PINS WHAT TWO PROCESSES AGREED ON. Given several recaptures of
    the same world, an exact Then is written only when they printed the same
    text; when they diverge, the twin's Then is their agreed PREFIX, flagged
    `prefix` so the replay asserts `starts_with` plus a longer observation.
    `None` when the recaptures agree on nothing — there is no observable to
    pin and the caller withdraws, which is the honest outcome rather than a
    test that asserts the empty string.

    WHY the whole render cannot be the Then. The capture runs in the probe
    binary and the replay in the test binary, so any part of a render that
    names the rendering PROCESS differs deterministically between them. Run
    `330242-0be2c3e1` (2026-09-03) emitted the twin as the whole perturbed
    render, and both clusters' only Debug probe came back
    `replay disagreed with the capture` and was withdrawn — the emitter
    working as designed on a Then that was never replayable.

    WHY two runs of the same build are not enough. Two runs of one probe
    binary print the same frames, so their agreement is the whole render,
    and the replay's narrowing then cuts it where the probe binary and the
    test binary part: after the crate's own frames, with the original's
    private fn names, lines and columns in the Then. The original passes
    that and no reconstruction with other line numbers can. The caller
    passes one recapture per :class:`ProbeBuild`, so the agreement drops what
    the build owns and keeps the crate's output (the message, the chain, the
    header that says a tail follows). It names no environment variable, no
    trait, no crate symbol and no runtime's format.

    A single recapture (a `str`, or one element) has nothing to agree with,
    so it keeps today's exact Then — the caller that wants the prefix
    behaviour passes the captures it took."""
    stdouts = ([perturb_stdout] if isinstance(perturb_stdout, str)
               else list(perturb_stdout))
    if not stdouts:
        return None
    then = outcome_from_stdout(spec, stdouts[0])
    if len(stdouts) > 1 and any(s != stdouts[0] for s in stdouts[1:]):
        values = [outcome_from_stdout(spec, out).value for out in stdouts]
        shared = agreed_prefix(values)
        if not shared:
            return None
        then = replace(then, value=shared, prefix=True)
    orig = replace(spec, required_env=env_pairs(unperturbed_env(perturb_env)))
    twin = replace(
        spec,
        behaviour_id=env_twin_id(spec.behaviour_id),
        then=then,
        required_env=env_pairs(perturb_env),
        source_ref=(f"{spec.source_ref}::env" if spec.source_ref else "env"),
    )
    return orig, classified(twin, default_cluster=spec.cfg_cluster)




def fills_non_format_class(probe: CapturedProbe) -> bool:
    """True when this block is answering IDENTITY / OPTION / BOUNDED_RESOURCE,
    so a Display observe is legitimate even while EXACT_FORMAT is also asked."""
    if identity_type_of(probe.when) is not None:
        return True
    if has_resource_boundary(probe.given):
        return True
    return observes_presence(probe.when)


def exact_format_ask_rejects(probe: CapturedProbe) -> str | None:
    """Why this block may not stand in for an EXACT_FORMAT ask, else None.

    The bound driver accepts any non-empty observe — a Display probe that
    moved under the env flip counts."""
    if fills_non_format_class(probe):
        return None
    obs = (probe.observe or "").strip().lower()
    if language_driver().exact_format_observe_ok(obs):
        return None
    return (
        "EXACT_FORMAT ask requires an observe tag naming the render; "
        f"got {obs or 'empty'}"
    )


def probe_unit_label(rec: FnRecord) -> str:
    """`Type::fn` for a method, `fn` for a free fn — the id the synthesis
    prompt names and the probe spec carries."""
    self_ty = impl_self_type(rec.container) if rec.container else None
    return f"{self_ty}::{rec.fn_name}" if self_ty else rec.fn_name


def _hidden_module_path(mod_path: str) -> bool:
    """A `__`-prefixed module segment (`__private`) — the crate-author
    convention for a `#[doc(hidden)] pub mod` that macros expand into and
    no user calls. HEURISTIC: the mod record does not carry `#[doc(hidden)]`,
    so the name is the only signal available here."""
    return any(seg.startswith("__") for seg in mod_path.split("::") if seg)


def _cfg_excluded(rec: FnRecord, cfg_set: CfgSet | None) -> bool:
    """True when one of the fn's own `#[cfg]` predicates evaluates FALSE under
    the cluster (`Error::thiserror_provide` is `error_generic_member_access`,
    which the stable cluster lists as false); undecidable stays included."""
    if cfg_set is None:
        return False
    return any(cfg_set.evaluate(pred) is False for pred in rec.cfg_predicates)


def is_public_crate_fn(
    rec: FnRecord,
    reachable_files: frozenset[str] | None,
    type_files: Mapping[str, str] | None,
    cfg_set: CfgSet | None,
    public_index: PublicIndex | None = None,
) -> bool:
    """True when an example crate can call `rec`: exactly `pub`, not a test,
    not under an aux dir or a hidden/cfg-dead module, and nameable from
    outside the crate.

    Nameable is read from ``public_index`` when one was measured (rustdoc's
    index, `fn_reachability.PublicSurface.item_names`: names by defining
    file): a free fn when the index lists it in its file, a method when it
    does and also lists its self type. That is the fact — a `pub use` from a
    private module makes an item nameable, and a mod-chain walk cannot see it
    (run `88434-2e3bb4d7`, hardcrate: 14 `pub use` lines, a 4-name glossary).
    Without an index, reachability through a public chain of `mod`
    declarations (``reachable_files``) stands in: a method's self type, or a
    free fn's file."""
    if not (rec.is_pub and rec.is_exactly_pub) or rec.is_test:
        return False
    if set(Path(rec.file).parts) & _AUX_DIRS:
        return False
    if _hidden_module_path(rec.mod_path) or _cfg_excluded(rec, cfg_set):
        return False
    if public_index:
        if rec.fn_name not in public_index.names_in(rec.file):
            return False
        self_ty = (impl_self_type(rec.container)
                   if rec.container.startswith("impl") else None)
        return self_ty is None or self_ty in public_index.names()
    if reachable_files is None:
        return True
    self_ty = impl_self_type(rec.container) if rec.container.startswith("impl") else None
    if self_ty is not None:
        # A method is callable iff its SELF TYPE is public: the impl may sit
        # in a private module (`mod error;` holds `impl Error`) while the type
        # is declared at the reachable root.
        declared_in = (type_files or {}).get(self_ty)
        return declared_in is not None and declared_in in reachable_files
    return rec.file in reachable_files


# --- class-driven target selection -------------------------------------------
#
# The classes a REPRESENTATION decides, with the phrasing the synthesis prompt
# asks in. Behaviours are transversal: "an error built from a string downcasts
# to `&str`" spans the constructor, the vtable, `object_downcast` and
# `Error::downcast`. Selecting fns cannot reach one, which is why the previous
# selector's alphabetical `[:max_targets]` produced probes for
# `Error::backtrace` / `Error::into_boxed_dyn_error` / `Error::reallocate_…`
# and nothing of any class. Per-unit reachability is unit tests' job.
CLASS_PROMPTS: Mapping[str, str] = {
    "IDENTITY": "the concrete type a downcast finds on a value this crate constructed "
    "— the TypeId round-trip, named by type",
    "TRAIT_OBJECT_IDENTITY": "the type observed THROUGH a trait object (`&dyn std::error::Error`), "
    "which a wrapper representation can answer differently",
    "EXACT_FORMAT": "an exact Debug rendering (`{:?}` or the alternate `{:#?}`), never Display — Display does not pin a render that moves with the environment",
    "BOUNDED_RESOURCE": "what happens at a size or capacity boundary, and just past it",
    "OPTION_SEMANTICS": "when an optional observable is present and when it is absent",
}



def class_prompts(*, pin_env_debug: bool) -> dict[str, str]:
    """Bound-driver class-ask phrasing."""
    return language_driver().class_prompts(pin_env_debug=pin_env_debug)


def class_targets(
    specs: Iterable[BehaviourSpec],
    class_of: Callable[[BehaviourSpec], str],
    *,
    max_targets: int,
    classes: Sequence[str] = (),
    prompts: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """`(class, what to write)` pairs for the classes this suite is THINNEST in,
    scarcest first, capped at `max_targets`.

    Scarcity, not absence, is the ordering: a class with zero specs and a class
    with two are both under-specified, and an absence can be an artifact of the
    classifier rather than a real hole — `CHAIN_SEQUENCE` and `DOWNCAST_OK` read
    zero on the live suite while `tests/test_chain.rs` contributes 16 specs under
    other kinds, and `DOWNCAST_OK` was deliberately retired on 2026-08-26 for
    carrying a bool without saying WHICH type. Ranking by count asks for more of
    what is thin without asserting that a zero is a gap.

    `class_of` is injected so the domain does not import the selector that reads
    a spec's class (`behaviour_select.behaviour_class_name`). `prompts` is the
    phrasing table; None uses :data:`CLASS_PROMPTS` (Debug-only EXACT_FORMAT).
    Pass :func:`class_prompts` with pin_env_debug off to restore Display-or-Debug."""
    if max_targets <= 0:
        return ()
    table = (language_driver().class_prompts(pin_env_debug=True)
             if prompts is None else prompts)
    wanted = [c for c in (classes or tuple(table)) if c in table]
    counts = dict.fromkeys(wanted, 0)
    for spec in specs:
        name = class_of(spec)
        if name in counts:
            counts[name] += 1
    order = sorted(wanted, key=lambda c: (counts[c], wanted.index(c)))
    return tuple((c, table[c]) for c in order[:max_targets])


def probe_targets(
    fn_records: Iterable[FnRecord],
    specs: Iterable[BehaviourSpec],
    *,
    max_targets: int,
    reachable_files: frozenset[str] | None = None,
    type_files: Mapping[str, str] | None = None,
    cfg_set: CfgSet | None = None,
    public_index: PublicIndex | None = None,
) -> tuple[str, ...]:
    """The externally-callable fns NO harvested example names in its code
    (:func:`example_code` — helpers, setup, when; whole-word on the bare fn
    name), as prompt labels, sorted, capped at `max_targets`. A fn an example
    runs is already observed by something that ran against the original; the
    probe budget goes to the rest. A fn named only in a test statement no
    harvested assertion depends on is NOT observed and stays a target (before
    2026-09-29 every example carried its whole test item, which counted it).

    `reachable_files` is `module_reachability.externally_reachable_files`
    over the same records — the files behind a PUBLIC chain of `mod`
    declarations — and `type_files` maps each `pub` type name to the file
    that declares it. A fn's own `pub` is not the authority: on the anyhow
    skeleton (2026-08-26, 132 records, 41 `pub` fns) the visibility flags
    alone offered 23 targets, and the first 12-slot batch was
    `Error::backtrace, Error::from_boxed, …, Mut::by_ref, Mut::cast,
    Mut::deref_mut, Mut::extend, Own::boxed, Own::by_mut, Own::by_ref` —
    `Own`/`Ref`/`Mut` are pub structs behind the private `mod ptr;`,
    `nightly.rs` sits behind a cfg-gated private `mod nightly;`, and
    `__private::format_err` is the macro support module. None of those can
    be named from an example crate, so every such probe fails to compile and
    the whole budget buys nothing. Nor is the impl's FILE the authority:
    anyhow declares `pub struct Error` in lib.rs and implements
    `impl Error { pub fn msg … }` in the private `mod error;`, so a
    file-only rule offered zero targets. A method counts when its self type
    is declared in a reachable file; a free fn when its own file is. None
    for `reachable_files` = no reachability known (every `pub` fn counts,
    the pre-fix behaviour). `cfg_set` is the cluster's cfg world: a fn whose
    own `#[cfg]` evaluates false there (`Error::thiserror_provide` on
    stable) cannot compile in that cluster's probe and is skipped."""
    if max_targets <= 0:
        return ()
    texts = [example_code(s) for s in specs]
    labels: set[str] = set()
    for rec in fn_records:
        if not is_public_crate_fn(rec, reachable_files, type_files, cfg_set,
                                  public_index):
            continue
        if any(contains_word(t, rec.fn_name) for t in texts):
            continue
        labels.add(probe_unit_label(rec))
    return tuple(sorted(labels))[:max_targets]


def batches(items: Sequence[str], size: int = PROBE_BATCH_SIZE) -> list[tuple[str, ...]]:
    """`items` in consecutive batches of at most `size` (one prompt each)."""
    size = max(1, size)
    return [tuple(items[i : i + size]) for i in range(0, len(items), size)]


def probe_behaviour_id(cfg_cluster: str, unit: str, n: int) -> str:
    return f"probe::{cfg_cluster}::{unit}::{n}"


def is_probe_spec(spec: BehaviourSpec) -> bool:
    return is_probe_id(spec.behaviour_id)


def probe_coverage_class(spec: BehaviourSpec) -> str:
    """Scarcity class for the probe ask.

    Harvested tests do not count: they re-read checks cargo already runs, and
    they pin the unperturbed world. A Display probe does not cover
    EXACT_FORMAT: Display of a message does not move with `RUST_BACKTRACE`,
    so a suite of them still has no Debug-under-captured-backtrace pin. Only
    a probe whose Then is Debug, alternate-Debug, or a chain sequence covers
    that class."""
    if not is_probe_spec(spec):
        return ""
    name = spec.klass.name
    if name == "OTHER":
        return ""
    if name == "EXACT_FORMAT":
        if not language_driver().exact_format_probe_covers(spec):
            return ""
    return name


def probe_spec(
    *,
    cfg_cluster: str,
    unit: str,
    n: int,
    program: str,
    stdout: str,
    default_cluster: str | None = None,
) -> BehaviourSpec:
    """LEGACY free-form capture: `given` = a whole program, Then = its stdout.

    Kept for replies that still arrive in the old shape. The record it makes is
    the one measured unusable — NORMALIZED_STDOUT is in no format or identity
    set, so `classified()` returns OTHER, `behaviours_pinned_by` can never join
    it, and t1.5's cap discards it. Prefer :func:`captured_spec`."""
    spec = BehaviourSpec(
        behaviour_id=probe_behaviour_id(cfg_cluster, unit, n),
        setup=program,
        when=unit,
        then=ObservableOutcome.stdout(normalize_stdout(stdout)),
        provenance=BehaviourProvenance.SYNTHESIZED,
        subkind=BehaviourSubKind.OUTCOME,
        fence=FenceDirective.RUN,
        cfg_cluster=cfg_cluster,
        source_ref=f"probe::{unit}",
        origin=ExampleOrigin(OriginKind.PROBE, "", unit, n),
    )
    return classified(spec, default_cluster=default_cluster or cfg_cluster)


def captured_spec(
    *,
    cfg_cluster: str,
    probe: CapturedProbe,
    n: int,
    stdout: str,
    default_cluster: str | None = None,
) -> BehaviourSpec:
    """A capture in the constrained shape: `given` = the SETUP statements,
    `when` = the single expression, Then = the observed value under its typed
    kind. `stdout` is the RAW capture; :func:`printed_value` removes the one
    newline `println!` added and nothing else, because the replay asserts this
    value against what `format!` produces.

    The program is not stored — :func:`render_probe_program` rebuilds it from
    these three whenever a runnable form is needed (the capture run itself, and
    `exercises_by_coverage`). An identity `when` stores the TYPE as the Then's
    value with `ok` from the printed boolean, because that is what makes the pin
    say WHICH TypeId the round-trip must preserve."""
    unit = probe.name or "unnamed"
    text = printed_value(stdout)
    kind = observe_kind(probe.observe, probe.when)
    if kind is OutcomeKind.TYPE_IDENTITY:
        then = ObservableOutcome.type_identity(identity_type_of(probe.when) or "?", ok=text.strip() == "true")
    elif kind is OutcomeKind.DEBUG_STRING:
        then = ObservableOutcome.debug(text)
    elif kind is OutcomeKind.ALT_DEBUG_STRING:
        then = ObservableOutcome.alt_debug(text)
    else:
        then = ObservableOutcome.display(text)
    spec = BehaviourSpec(
        behaviour_id=probe_behaviour_id(cfg_cluster, unit, n),
        setup=probe.given.rstrip(),
        when=probe.when,
        then=then,
        provenance=BehaviourProvenance.SYNTHESIZED,
        subkind=BehaviourSubKind.OUTCOME,
        fence=FenceDirective.RUN,
        cfg_cluster=cfg_cluster,
        source_ref=f"probe::{unit}",
        origin=ExampleOrigin(OriginKind.PROBE, "", unit, n),
    )
    return classified(spec, default_cluster=default_cluster or cfg_cluster)


def program_for(spec: BehaviourSpec, *, crate: str = "") -> str:
    """The runnable program for ANY probe spec — the stored one for a legacy
    free-form capture, a rendered one for the constrained shape.

    `exercises_by_coverage` re-runs this. Before it existed that caller passed
    the stored text straight to `cargo run --example`, so a setup that stopped
    being a whole program would have failed every probe's exercises run with a
    log line and no other trace."""
    if spec.then is not None and spec.then.kind is OutcomeKind.NORMALIZED_STDOUT:
        return spec.setup
    observe = {OutcomeKind.DEBUG_STRING: "debug", OutcomeKind.ALT_DEBUG_STRING: "alt_debug"}.get(
        spec.then.kind if spec.then else OutcomeKind.DISPLAY_STRING, "display"
    )
    return render_probe_program(spec.setup, spec.when, observe, crate=crate)


def render_replay_test(spec: BehaviourSpec, *, fn_name: str) -> str:
    """The REPLAY role: the same capture as a `#[test]` asserting the value it
    was observed to have. `""` when the spec cannot be replayed (a legacy
    free-form blob, or no Then).

    This is the half that makes a capture part of the cargo-test oracle. The
    expected value is the OBSERVED one — never a model's guess — which is the
    whole reason the capture route exists beside t0c's.

    `required_env` is written as `set_var` BEFORE the setup so construction-time
    capture (`backtrace!()` in constructors) and format-time reads both see
    it, and the reconstruction sweep's `cargo test` does not have to inherit
    the host env."""
    then = spec.then
    if then is None or then.kind is OutcomeKind.NORMALIZED_STDOUT:
        return ""
    env_lines = "".join(
        f"    std::env::set_var({rust_string_literal(k)}, {rust_string_literal(v)});\n" for k, v in spec.required_env
    )
    body = spec.setup.rstrip()
    setup = (body + "\n") if body else ""
    if then.kind is OutcomeKind.TYPE_IDENTITY:
        check = f"    assert!({spec.when});" if then.ok else f"    assert!(!({spec.when}));"
    elif then.prefix or spec.required_env:
        # A world-pinned capture REPORTS what it rendered when it fails, so
        # the emitter can narrow the Then to the text the capture and the
        # replay share instead of withdrawing the capture. The tail of a
        # prefix Then belongs to the rendering process and is asserted to
        # EXIST, never to match.
        fmt = _KIND_TO_FMT.get(then.kind, "{}")
        lit = rust_string_literal(then.value)
        mark = f"{OBSERVED_OPEN}{fn_name}{OBSERVED_MID}{{actual}}{OBSERVED_CLOSE}"
        head = f'    let actual = format!("{fmt}", {spec.when});\n'
        if then.prefix:
            check = (head
                     + f'    assert!(actual.starts_with({lit}), "{mark}");\n'
                     + f'    assert!(actual.len() > {lit}.len(), "{mark}");')
        else:
            check = head + f'    assert!(actual == {lit}, "{mark}");'
    else:
        fmt = _KIND_TO_FMT.get(then.kind, "{}")
        lit = rust_string_literal(then.value)
        check = f'    assert_eq!(format!("{fmt}", {spec.when}), {lit});'
    # The rules this capture is an example of, above the test: a failing
    # replay then shows, in the test source a fix prompt already quotes,
    # the sentence the body has to satisfy and not only a value.
    rules = "".join(f"// rule: {' '.join(r.sentence.split())}\n" for r in spec.rules)
    return f"{rules}#[test]\nfn {fn_name}() {{\n{env_lines}{setup}{check}\n}}\n"


def rust_string_literal(text: str) -> str:
    """`text` as a Rust literal. A raw string when it holds a backslash or a
    quote and no `"#` sequence — the captured values are exact renderings and
    escaping them by hand is how an expected value stops matching itself."""
    if ("\\" in text or '"' in text) and '"#' not in text:
        return 'r#"' + text + '"#'
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return '"' + out + '"'
