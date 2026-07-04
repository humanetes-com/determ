"""Starvation-cause classification (july_plan P1).

Why did a fill die? The pipeline PUSHES heuristic context slices at the model;
when the slice misses the one fact the fn needed, the model hallucinates and
the fix loop thrashes. This module names the failure modes as a closed set and
classifies one failed attempt (prompt, response, error) into a
:class:`StarvationCause` — pure text analysis, no I/O, no regex (backtick /
word-boundary walks, `path_leaf` from bound_mismatch for trait/type paths).

The miner (`application/starvation_report.py` + `tools/starvation_miner.py`)
aggregates these into the ranked evidence table that orders july_plan P2-P4.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.bound_mismatch import (
    detect_frozen_bound_mismatch,
    path_leaf,
)
from pipeline_core.domain.test_results import strip_ansi


class StarvationCause(StrEnum):
    """The closed set of context-starvation failure modes."""

    SYMBOL_ABSENT = "symbol_absent"        # needed value/fn ident never in prompt
    TYPE_DEF_ABSENT = "type_def_absent"    # needed type's definition never in prompt
    TEST_ABSENT = "test_absent"            # test failed but its source not shown
    ERROR_TRUNCATED = "error_truncated"    # error excerpt is a mid-diagnostic tail
    DESIGN_CONFLICT = "design_conflict"    # signature/scheme disagreement (E0053/E0046/E0407)
    RESPONSE_UNUSED = "response_unused"    # model answered but nothing was spliced
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FillPostMortem:
    """One failed fill, classified. `unit` is `file::fn` or `file::container`."""

    unit: str
    cluster: str
    attempts: int
    cause: StarvationCause
    evidence: str


def has_word(text: str, word: str) -> bool:
    """Whole-word presence: `word` occurs as a maximal ``[A-Za-z0-9_]+`` run."""
    if not word:
        return False
    start = 0
    n = len(text)
    w = len(word)
    while True:
        i = text.find(word, start)
        if i == -1:
            return False
        before_ok = i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")
        after = i + w
        after_ok = after >= n or not (text[after].isalnum() or text[after] == "_")
        if before_ok and after_ok:
            return True
        start = i + 1


def _backticked(line: str) -> list[str]:
    """The `-quoted spans of one line, in order."""
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        a = line.find("`", i)
        if a == -1:
            break
        b = line.find("`", a + 1)
        if b == -1:
            break
        out.append(line[a + 1:b])
        i = b + 1
    return out


# Lines whose FIRST backticked span names the ident rustc could not resolve.
_MISSING_IDENT_MARKERS = (
    "cannot find", "no method named", "failed to resolve",
    "not found in this scope", "no function or associated item named",
    "no variant or associated item named", "unresolved import",
)
# A well-formed excerpt starts at a diagnostic head, not mid-snippet.
_DIAGNOSTIC_HEADS = (
    "error", "warning", "note", "help", "test ", "thread ", "running",
    "Errors in target file", "FILE NOT FOUND", "TIMEOUT", "Compiling",
    "Checking", "Finished", "cargo", "failures:", "$",
)
_DESIGN_CONFLICT_CODES = ("E0053", "E0046", "E0407")
_TEST_FAILURE_MARKS = ("test result: FAILED", "panicked at", "assertion",
                       "... FAILED")


def _ident_root(span: str) -> str:
    """`Own<T>` → `Own`; `a::b::c` → `c`; `E: Trait` → `E` (the subject)."""
    colon = span.find(": ")
    if colon != -1:
        span = span[:colon]
    return path_leaf(span)


def _type_like(ident: str) -> bool:
    return bool(ident) and ident[0].isupper()


def classify_starvation(
    prompt_text: str, response_text: str, error_text: str, *,
    container: str = "",
) -> tuple[StarvationCause, str]:
    """Classify one failed attempt. Returns (cause, evidence) where evidence
    is the ident or line that drove the verdict. Precedence: content verdicts
    (missing ident/type, test-without-source, design conflict) beat the
    truncation check — a cut excerpt that still shows the cause is classified
    by the cause. `container` (the frozen impl header, when known) enables the
    debt-#28 frozen-bound-mismatch detector."""
    err = strip_ansi(error_text or "")   # previews carry cargo's SGR colors
    prompt = prompt_text or ""
    response = response_text or ""

    # 0) No error at all: the fill died WITHOUT a compile/test verdict — the
    # model's reply was produced but never applied (splice refused, routing
    # dropped every body, empty/no FN blocks). A pipeline loss, not a model one.
    if not err.strip():
        if response.strip():
            return (StarvationCause.RESPONSE_UNUSED,
                    "no error captured — reply produced but nothing spliced")
        return StarvationCause.UNKNOWN, "no error and no response captured"

    # 1) Frozen-bound mismatch (debt #28): the impl header freezes a param to
    # one trait while the compiler demands another — no body can fix it.
    if container:
        fbm = detect_frozen_bound_mismatch("", container, err)
        if fbm is not None:
            return (StarvationCause.DESIGN_CONFLICT,
                    f"{fbm.param}: frozen {'+'.join(fbm.frozen)} "
                    f"but E0277 requires {fbm.required}")

    # 2) Test failed but the prompt never showed the failing test's source.
    if any(m in err for m in _TEST_FAILURE_MARKS):
        for name in _failed_test_names(err):
            if not has_word(prompt, name):
                return StarvationCause.TEST_ABSENT, name

    for line in err.splitlines():
        # 2) Frozen-scheme disagreements are design, not lookup, failures.
        if any(code in line for code in _DESIGN_CONFLICT_CODES):
            return StarvationCause.DESIGN_CONFLICT, line.strip()
        # 2b) E0609 `no field` — the model guessed a PRIVATE field name. If the
        # owning type's definition never reached the prompt that is starvation;
        # if it did, the model contradicted the frozen layout (P4 evidence).
        if "no field" in line and "`" in line:
            spans = _backticked(line)
            if len(spans) >= 2:
                field, owner = spans[0], _ident_root(spans[1])
                if owner and not has_word(prompt, owner):
                    return StarvationCause.TYPE_DEF_ABSENT, owner
                if owner:
                    return (StarvationCause.DESIGN_CONFLICT,
                            f"invented field `{field}` on `{owner}`")
        # 3) rustc names an ident it cannot resolve → was it ever in the prompt?
        if any(m in line for m in _MISSING_IDENT_MARKERS):
            spans = _backticked(line)
            if spans:
                ident = _ident_root(spans[0])
                if ident and not has_word(prompt, ident) and (
                        has_word(response, ident) or has_word(err, ident)):
                    cause = (StarvationCause.TYPE_DEF_ABSENT if _type_like(ident)
                             else StarvationCause.SYMBOL_ABSENT)
                    return cause, ident
        # 4) Type-level mismatch naming a type the prompt never defined.
        if "E0308" in line or "E0277" in line:
            for span in _backticked(line):
                ident = _ident_root(span)
                if _type_like(ident) and not has_word(prompt, ident):
                    return StarvationCause.TYPE_DEF_ABSENT, ident

    # 5) Nothing classifiable AND the excerpt starts mid-diagnostic. The
    # focused-error header renders as `# Errors in target file:` — strip the
    # comment lead before matching heads.
    first = next((ln for ln in err.splitlines() if ln.strip()), "")
    head = first.lstrip().lstrip("#").lstrip()
    if head and not any(head.startswith(h) for h in _DIAGNOSTIC_HEADS):
        return StarvationCause.ERROR_TRUNCATED, first.strip()[:120]

    return StarvationCause.UNKNOWN, _salient_line(err)


def _salient_line(err: str) -> str:
    """Evidence for UNKNOWN verdicts: the first real diagnostic line —
    skipping headers and cargo's `could not compile` summary — else the first
    non-empty line."""
    fallback = ""
    for ln in err.splitlines():
        s = ln.strip()
        if not s:
            continue
        if not fallback:
            fallback = s
        if s.startswith("error") and "could not compile" not in s:
            return s[:120]
    return fallback[:120]


def _failed_test_names(err: str) -> list[str]:
    """Test fn names from `test path::name ... FAILED` lines and the
    `failures:` list block."""
    names: list[str] = []
    in_failures = False
    for line in err.splitlines():
        s = line.strip()
        if s == "failures:":
            in_failures = True
            continue
        if in_failures:
            if not s or s.startswith("test result"):
                in_failures = False
            elif " " not in s:
                names.append(s.rsplit("::", 1)[-1])
            continue
        if s.startswith("test ") and s.endswith("... FAILED"):
            path = s[len("test "):].split(" ", 1)[0]
            names.append(path.rsplit("::", 1)[-1])
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out
