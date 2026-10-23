"""How a test fails, as a value two trees can be compared on (pure domain).

The reference floor excuses a red test because the original crate fails it
too. Until 2026-10-22 it compared names only: `(binary, test)` in the
original's failed set was excused whatever this tree's failure said. On the
finished run `391506-3403a6a7` the one red test was trybuild's `ui` under
`nightly`, and its failure here happened to be the original's byte for byte
(a scratch alone run of both trees, 2026-10-22). The gate could not have
told: a reconstruction that dropped a re-export prints a different path in
the same `no-impl.rs` mismatch, another `ui` case going from `ok` to
`mismatch` still fails the one libtest test `ui`, and either would have
closed green. So an excuse needs the failure itself, not its name.

**What "the failure" is.** The output of the test run alone in its own
process (`--exact NAME --test-threads=1`), stdout and stderr. libtest's
`---- NAME stdout ----` block is not enough: in that same capture the `ui`
block holds only `1 of 7 tests failed`, while the per-case lines and the
EXPECTED/ACTUAL blocks go to the process's stderr, which a whole-suite
capture cannot attribute to a test. A test with no executable to run alone
(a doc test, run by rustdoc) falls back to its failure block, which for a
doc test is rustdoc's whole report.

**Normalization, rule by rule, each from a measured difference.** On the
2026-10-22 pair, after these rules the two `ui` outputs are 94 identical
lines; before them they differ in:
- ANSI colour escapes (trybuild colours its report);
- `thread 'ui' (1974125)`: the thread id;
- `finished in 2.34s`: the duration;
- cargo's status lines (`Compiling …`, `Checking …`, `Finished …`,
  `Running …`), which trybuild's inner build prints with each dependency's
  resolved version: the two trees resolved different lockfiles;
- every `warning:` diagnostic, through the blank line that ends it: the
  reconstruction printed 1,218 lines of `unexpected cfg condition name`
  warnings, ending with cargo's `(lib) generated 93 warnings` trailer,
  because it has no build script declaring its cfg names, and the original
  printed none. A warning cannot fail a test;
- a dependency's registry path carries its version and its line numbers
  (`…/trybuild-1.0.122/src/run.rs:103:13` against `…/trybuild-1.0.120/…`):
  kept as the crate name and the file;
- a location in the crate's own `src/` keeps the file and drops line and
  column: the reconstruction's line numbering is not the original's (the
  same reason `reference_floor.normalize_pair` strips a doc test's line);
- the tree roots, the target dir and the home directory are replaced by one
  token, since the two trees live in different directories;
- hex addresses.
Any other nondeterminism is not given a rule here. The reference measures
each failure twice, and a failure whose two readings differ is not used as
evidence (:class:`ReferenceFailure`).

Nothing here names a crate, a test, a trait or a test harness: the rules key
on cargo's, libtest's and rustc's own output shapes.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "FailedReading",
    "FailureSignature",
    "FailureToRead",
    "ReferenceFailure",
    "SignatureReader",
    "alone_args",
    "signature_of",
]

Pair = tuple[str, str]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_THREAD_ID = re.compile(r"(thread '[^']*') \(\d+\)")
_DURATION = re.compile(r"finished in \d+(?:\.\d+)?s")
_HEX = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
# `…/registry/src/<index dir>/<crate>-<version>/<path>[:line[:col]]`
_REGISTRY = re.compile(
    r"\S*/registry/src/[^/\s]+/([A-Za-z0-9_.\-]+?)-\d+\.\d+\.\d+[^/\s]*/"
    r"([^\s:]+)(?::\d+(?::\d+)?)?")
# A location in the crate's own sources: `src/x.rs:12:5` (also `$ROOT/src/…`).
_SRC_LOCATION = re.compile(r"((?:^|[\s(`'\"=/])src/[^\s:]+\.rs):\d+(?::\d+)?")
_DOC_LINE = re.compile(r" \(line \d+\)")
# The verbs cargo right-aligns on its status lines.
_CARGO_STATUS = frozenset({
    "Adding", "Blocking", "Building", "Checking", "Compiling", "Documenting",
    "Downloaded", "Downloading", "Finished", "Fresh", "Locking", "Packaging",
    "Running", "Updating", "Waiting"})
_ROOT_TOKEN = "$ROOT"


@dataclass(frozen=True, slots=True)
class FailureSignature:
    """The normalized text of one test's failure."""

    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]

    def first_difference(self, other: FailureSignature) -> str:
        """Where this failure first departs from `other`, for a log line:
        the line number and THIS tree's line only. The other side is the
        original's output, which can quote the original's source (a rustc
        note pointing into the crate), so it is compared and never printed."""
        mine, theirs = self.text.splitlines(), other.text.splitlines()
        for i in range(max(len(mine), len(theirs))):
            a = mine[i] if i < len(mine) else "<end>"
            b = theirs[i] if i < len(theirs) else "<end>"
            if a != b:
                return f"line {i + 1} differs; here: `{a.strip()[:160]}`"
        return ""


def _is_cargo_status(line: str) -> bool:
    head = line.lstrip().split(" ", 1)[0]
    return head in _CARGO_STATUS and line.startswith(" ") and " " in line.strip()


def _drop_warnings(lines: list[str]) -> list[str]:
    """Every `warning:` diagnostic, from its head line through the blank line
    rustc ends a diagnostic with."""
    out: list[str] = []
    skipping = False
    for ln in lines:
        if skipping:
            if not ln.strip():
                skipping = False
            continue
        if ln.startswith("warning:") or ln.startswith("warning["):
            skipping = True
            continue
        out.append(ln)
    return out


def signature_of(output: str, *, roots: Iterable[str] = ()) -> FailureSignature:
    """The one normalization (module docstring). `roots` are the absolute
    directories the run lived in (the tree, its target dir, the home dir)."""
    text = _ANSI.sub("", output)
    for root in sorted({r.rstrip("/") for r in roots if r and len(r) > 1},
                       key=len, reverse=True):
        text = text.replace(root, _ROOT_TOKEN)
    text = _REGISTRY.sub(lambda m: f"$REGISTRY/{m.group(1)}/{m.group(2)}", text)
    text = _SRC_LOCATION.sub(r"\1", text)
    text = _THREAD_ID.sub(r"\1", text)
    text = _DURATION.sub("finished in Ts", text)
    text = _DOC_LINE.sub(" (line _)", text)
    text = _HEX.sub("0xADDR", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if not _is_cargo_status(ln)]
    lines = _drop_warnings(lines)
    kept: list[str] = []
    for ln in lines:
        if not ln and (not kept or not kept[-1]):
            continue
        kept.append(ln)
    while kept and not kept[-1]:
        kept.pop()
    return FailureSignature("\n".join(kept))


def alone_args(name: str) -> list[str]:
    """The harness arguments that run exactly `name` in its own process."""
    return ["--exact", name, "--test-threads=1"]


@dataclass(frozen=True, slots=True)
class ReferenceFailure:
    """How the original failed one test, read twice in two processes. Only
    a failure whose two readings agree is evidence; one that moved between
    runs, or that could not be read, is excused by its name alone and the
    log says so."""

    first: FailureSignature | None = None
    second: FailureSignature | None = None

    @property
    def stable(self) -> bool:
        return self.first is not None and self.first == self.second


@dataclass(frozen=True, slots=True)
class FailureToRead:
    """One failing test whose failure must be read: its comparison key, its
    libtest name in this tree, the executable that runs it alone (None for a
    doc test), the world it failed in, and its failure block in the shared
    capture (the fallback when there is no executable)."""

    pair: Pair
    label: str
    name: str
    exe_path: str | None
    world: str = ""
    body: str = ""


#: Reads the failures of `todo` in this tree: pair -> signature, None when
#: the failure could not be read (or the test passed when run alone).
SignatureReader = Callable[[Sequence[FailureToRead]], Mapping[Pair, "FailureSignature | None"]]


@dataclass(frozen=True, slots=True)
class FailedReading:
    """This tree's failing tests and, for those the floor must compare, how
    each failed. A pair with no entry in `signatures` was not read, and an
    unread failure cannot be excused by a reference failure that has
    evidence: the judge fails closed on it."""

    pairs: frozenset[Pair]
    signatures: Mapping[Pair, FailureSignature | None] = field(default_factory=dict)
