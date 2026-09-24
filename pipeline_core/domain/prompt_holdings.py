"""What a prompt already holds — the `<FILE>` grammar its writers share, the
contents index a reader gets up front, and the answer to a request for a
file the conversation already carries whole.

Why this exists, measured on the record (`scripts/prompt_sufficiency_census.py`
over every fill since 2026-09-01, computed 2026-09-24): the model asked for
context 690 times, and 302 of those asks named a file or type the prompt had
already rendered. Per-fn prompt `956e2150…` (96,032 chars) held all of
`src/error.rs`, `pub fn msg` included, and its thinking said "Let me request
the error module to see how Error is created". Nothing in any fill prompt said
what the prompt contained, and the resolver re-sent the raw file — with the
inactive `#[cfg]` arms the prompt's own view had filtered out — so the answer
was the same code plus noise. Of the 154 per-fn and session file asks whose
answer the record shows, 17 were code-identical to the view already held.

The contract is typed at the WRITER. Every fill-prompt `<FILE>` block says
whether it is the whole file (`view="whole"`: every item of the file as the
cluster compiles it; comments, docs and inactive cfg arms may be elided) or a
part (`view="part"`: a slice, signatures only, or a condensed or truncated
rendering). A block without the attribute reads as UNKNOWN and is never
treated as whole — a new writer that forgets to declare its view makes the
index say less, never lie. The session's slice file renders
`<FILE path="src/error.rs" editable="…">` with 240 of the file's 677 code
lines when it does not fit (2026-09-23); read as whole, it would have told the
model a condensed view was the file.

Pure: no I/O. Rust syntax comes from `rust_fn_headers` / `rust_lex`, the same
comment- and string-aware scanners the splicer uses.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .context_guard import TRIM_MARKER
from .rust_fn_headers import fn_headers
from .rust_lex import code_mask


class FileView(StrEnum):
    """How much of a file one `<FILE>` block renders."""

    WHOLE = "whole"
    PART = "part"
    UNKNOWN = "unknown"   # a writer that did not declare it


def file_block(path: str, text: str, *, view: FileView, attrs: str = "") -> str:
    """The one way a fill prompt renders a file: `<FILE path=… view=…>`.

    `view` is required, so a writer cannot render a file without deciding
    what it is. `attrs` carries writer-specific attributes (`editable="…"`)
    verbatim, already formatted with a leading space."""
    if view is FileView.UNKNOWN:
        raise ValueError("a writer must declare whole or part")
    return (f'<FILE path="{path}" view="{view.value}"{attrs}>\n'
            f'{text.rstrip(chr(10))}\n</FILE>')


@dataclass(frozen=True, slots=True)
class HeldFile:
    path: str
    view: FileView
    text: str

    @property
    def lines(self) -> int:
        return len(self.text.splitlines())


@dataclass(frozen=True, slots=True)
class Holdings:
    """What a prompt (or a whole transcript) already carries."""

    files: tuple[HeldFile, ...] = ()
    tests: tuple[str, ...] = ()
    types: tuple[str, ...] = ()

    def whole_view(self, path: str) -> HeldFile | None:
        """The LAST whole view of `path` (a later view is the fresher one)."""
        for f in reversed(self.files):
            if f.view is FileView.WHOLE and _same_path(f.path, path):
                return f
        return None

    @property
    def empty(self) -> bool:
        return not (self.files or self.tests or self.types)


# Attributes are ` name="value"` pairs; a value may hold `>` (the session's
# `editable="fmt (impl<A> Foo<A>)"`), so the tag ends at the first `>` that is
# not inside quotes.
_FILE_RE = re.compile(
    r'<FILE path="([^"]+)"((?:\s+[\w-]+="[^"]*")*)\s*>\n?(.*?)\n?</FILE>', re.S)

_TEST_RE = re.compile(r'<TEST path="([^"]+)"[^>]*>')
_TYPES_RE = re.compile(r"<TYPES_IN_SCOPE>(.*?)</TYPES_IN_SCOPE>", re.S)
# A record's definition line as `render_types_section` prints it: at column 0,
# `kind Name…` (doc and attribute lines above it start with `///` / `#[`).
_TYPE_LINE_RE = re.compile(
    r"^(?:external\s+)?(struct|enum|union|trait|type)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M)
_VIEW_RE = re.compile(r'\bview="([a-z]+)"')


def _same_path(a: str, b: str) -> bool:
    a, b = a.lstrip("./"), b.lstrip("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def read_holdings(text: str) -> Holdings:
    """Parse the blocks every fill-prompt writer emits. Only these grammars
    count as holding something; prose that quotes a file does not."""
    files: list[HeldFile] = []
    for m in _FILE_RE.finditer(text):
        v = _VIEW_RE.search(m.group(2))
        try:
            view = FileView(v.group(1)) if v else FileView.UNKNOWN
        except ValueError:
            view = FileView.UNKNOWN
        body = m.group(3)
        if view is FileView.WHOLE and TRIM_MARKER in body:
            view = FileView.PART   # declared whole, then cut to fit
        files.append(HeldFile(m.group(1), view, body))
    tests = tuple(dict.fromkeys(m.group(1) for m in _TEST_RE.finditer(text)))
    types: list[str] = []
    for block in _TYPES_RE.findall(text):
        types.extend(m.group(2) for m in _TYPE_LINE_RE.finditer(block))
    return Holdings(tuple(files), tests, tuple(dict.fromkeys(types)))


CONTENTS_OPEN = "<PROMPT_CONTENTS>"
CONTENTS_CLOSE = "</PROMPT_CONTENTS>"


def render_contents(holdings: Holdings, *, max_types: int = 80) -> str:
    """The index a request-capable prompt opens with. "" when it holds
    nothing worth indexing.

    It states what is below and whether a file is whole, and nothing else:
    the model reads it before deciding to ask, which is the point — a request
    for something listed here costs a round-trip that re-sends the whole
    prompt."""
    if holdings.empty:
        return ""
    lines = [CONTENTS_OPEN,
             "Already in this prompt — read it there before requesting it:"]
    seen: dict[str, HeldFile] = {}
    for f in holdings.files:
        prev = seen.get(f.path)
        # One line per path: a whole view outranks any part of the same file.
        if prev is None or (f.view is FileView.WHOLE
                            and prev.view is not FileView.WHOLE):
            seen[f.path] = f
    if seen:
        lines.append("files:")
        for f in seen.values():
            what = {FileView.WHOLE: "the whole file",
                    FileView.PART: "part of the file",
                    FileView.UNKNOWN: "a rendering of the file"}[f.view]
            # Named without tag syntax: this index sits in the same text
            # `read_holdings` parses, and a `<FILE path=…>` here would open a
            # block that is not one.
            lines.append(f"  {f.path} — {what}, {f.lines} lines, in its "
                         "FILE block")
    if holdings.tests:
        lines.append("test sources: " + ", ".join(holdings.tests))
    if holdings.types:
        shown = holdings.types[:max_types]
        more = len(holdings.types) - len(shown)
        lines.append("type definitions in TYPES_IN_SCOPE: " + ", ".join(shown)
                     + (f" (+{more} more)" if more > 0 else ""))
    lines.append(CONTENTS_CLOSE)
    return "\n".join(lines)


def request_capable(text: str) -> bool:
    """A prompt that tells the model it may reply with a `<REQUEST …/>` —
    the only prompts where an index can save an ask."""
    return '<REQUEST kind="' in text


def contents_index(text: str) -> str:
    """The index `text` should open with: "" when it cannot request (an
    index there saves no ask), holds nothing, or already opens with one."""
    if text.startswith(CONTENTS_OPEN) or not request_capable(text):
        return ""
    return render_contents(read_holdings(text))


def with_contents(text: str) -> str:
    """`text` opened by its own index. Idempotent."""
    index = contents_index(text)
    return f"{index}\n\n{text}" if index else text


# ── the item outline and what changed since a whole view was shown ──────────

_ITEM_RE = re.compile(
    r"^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:(?:unsafe|async|const|default)\s+)*"
    r"(struct|enum|union|trait|impl\b|type|mod|macro_rules!)\s*([^\n{;]*)")


@dataclass(frozen=True, slots=True)
class OutlineItem:
    line: int          # 1-based, in the text it was read from
    header: str        # the declaration line, without the body


def outline(text: str) -> tuple[OutlineItem, ...]:
    """Item headers in source order: fns (every one the splicer would see)
    and type / impl / trait / mod / macro declarations. Comment- and
    string-aware: a `fn` inside a doc example is not an item."""
    mask = code_mask(text)
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)

    def line_of(pos: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    items: dict[int, OutlineItem] = {}
    for h in fn_headers(text, line_anchored=False):
        ln = line_of(h.start)
        head = " ".join(text[h.start:h.brace_open].split())
        items[ln] = OutlineItem(ln, head)
    for ln_idx, start in enumerate(starts):
        end = text.find("\n", start)
        line = text[start:end if end != -1 else len(text)]
        first = len(line) - len(line.lstrip())
        # `mask` is True on comment/string characters.
        if start + first >= len(mask) or mask[start + first]:
            continue
        m = _ITEM_RE.match(line)
        if m and (ln_idx + 1) not in items:
            items[ln_idx + 1] = OutlineItem(
                ln_idx + 1, " ".join(line.strip().rstrip("{").split())[:160])
    return tuple(items[k] for k in sorted(items))


def _fn_bodies(text: str) -> dict[str, list[str]]:
    """Declaration header -> the code of every body with that header, in
    order. Same-header fns are the norm (`fmt` in each impl), so the value
    is a list. Whitespace is collapsed; nothing else is normalised — the
    view's annotations and condensed docs sit OUTSIDE bodies, so a body
    differs only when it was rewritten."""
    mask = code_mask(text)   # True on comment/string characters
    out: dict[str, list[str]] = {}
    for h in fn_headers(text, line_anchored=False):
        depth, i = 1, h.brace_open + 1
        while i < len(text) and depth > 0:
            if not mask[i]:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
            i += 1
        if depth:
            continue
        body = text[h.brace_open + 1:i - 1]
        head = " ".join(text[h.start:h.brace_open].split())
        out.setdefault(head, []).append(" ".join(body.split()))
    return out


def changed_since(view: str, disk: str) -> tuple[str, ...]:
    """Fn headers whose body in `view` no longer exists on `disk`.

    A fill rewrites bodies and never adds or removes an item, so a held body
    that is not among the disk's bodies of the same header was rewritten
    after the view was rendered. Items present only on disk are not
    changes: the view is the cluster's, and inactive cfg arms are dropped
    from it on purpose."""
    now = _fn_bodies(disk)
    out: list[str] = []
    for head, bodies in _fn_bodies(view).items():
        on_disk = now.get(head)
        if on_disk is None:
            continue
        if any(b not in on_disk for b in bodies):
            out.append(head)
    return tuple(out)


def disk_items_for(disk: str, headers: Sequence[str]) -> str:
    """The disk text of every fn whose header is in `headers`."""
    wanted = set(headers)
    mask = code_mask(disk)
    parts: list[str] = []
    for h in fn_headers(disk, line_anchored=False):
        head = " ".join(disk[h.start:h.brace_open].split())
        if head not in wanted:
            continue
        depth, i = 1, h.brace_open + 1
        while i < len(disk) and depth > 0:
            if not mask[i]:
                if disk[i] == "{":
                    depth += 1
                elif disk[i] == "}":
                    depth -= 1
            i += 1
        parts.append(disk[h.start:i])
    return "\n\n".join(parts)


def held_file_answer(path: str, held: HeldFile, disk: str) -> str:
    """The answer to a whole-file request for a file the conversation already
    carries whole: where it is, what changed on disk since, and an outline
    with the disk's line numbers so a `from_line` re-read lands where the
    model means."""
    stale = changed_since(held.text, disk)
    # No tag syntax: this answer joins the conversation that `read_holdings`
    # parses, and a quoted `<FILE …>` opener would read as a whole view.
    lines = [f"{path} is already in this conversation as the whole file: its "
             f'FILE block above, view "whole" ({held.lines} lines). It is not '
             "sent again."]
    if stale:
        lines.append("These bodies were rewritten on disk after it was shown; "
                     "their current text:")
        lines.append(disk_items_for(disk, stale))
    else:
        lines.append("Nothing in it has changed on disk since.")
    items = outline(disk)
    if items:
        lines.append(f"Its items (line numbers of {path} on disk):")
        lines.extend(f"  L{it.line}: {it.header}" for it in items)
    lines.append(f'To read lines again, reply <REQUEST kind="file" arg="{path}" '
                 'from_line="N"/>.')
    return "\n".join(lines)
