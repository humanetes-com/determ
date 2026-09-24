"""Pull-protocol resolver + bounded ask loop — application.

`PullSources` is the ISP seam: five injected reads, every one of them served
from what the reconstruction is allowed to see (workspace tree, tests, baked
type records, cargo output). `workspace_file_reader` builds the FILE read with
structural path containment — any path resolving outside the reconstruction
tree is refused, so the upstream crate source is unreachable by construction
rather than by policy.

`ConversationRequests` answers the requests of one conversation: at most
`PullBudget.remaining` answered requests per turn — ALL of a reply's requests
are served, not just its last — then one notice that tells a still-asking
model the budget is spent. The conversation that carries the answers is
`unit_conversation.UnitConversation`; the request turn the model wrote stays
in it, above the answer.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from pipeline_core.domain.context_pull import (
    ContextPull,
    PullAnswer,
    PullBudget,
    PullKind,
    extract_context_pulls,
    window_lines,
)
from pipeline_core.domain.prompt_holdings import held_file_answer, read_holdings
from pipeline_core.prompts.pull_menu import (
    render_pull_budget_spent,
    render_pull_followups,
)

# Payload caps (chars) — generous; `truncated` is flagged so the model knows.
_CAPS: dict[PullKind, int] = {
    PullKind.FILE: 40_000,
    PullKind.TEST_SOURCE: 20_000,
    PullKind.TYPE_DEF: 8_000,
    PullKind.FULL_ERROR: 30_000,
    PullKind.MACRO_EXPANSION: 40_000,
}


@dataclass(frozen=True, slots=True)
class PullSources:
    """What the composition site can serve. `None` disables a kind — the
    menu only advertises servable kinds."""

    read_file: Callable[[str], str | None]
    find_tests: Callable[[str], str]
    type_def: Callable[[str], str]
    full_error: Callable[[], str]
    expand_macro: Callable[[str], str] | None = None

    def available_kinds(self) -> tuple[PullKind, ...]:
        kinds = [PullKind.FILE, PullKind.TEST_SOURCE, PullKind.TYPE_DEF,
                 PullKind.FULL_ERROR]
        if self.expand_macro is not None:
            kinds.append(PullKind.MACRO_EXPANSION)
        return tuple(kinds)


def workspace_file_reader(root: Path) -> Callable[[str], str | None]:
    """FILE-kind reader locked to the reconstruction tree: the resolved path
    must stay under `root` and be a `.rs` file, so no `../` walk can reach the
    upstream source the reconstruction is being measured against."""
    resolved_root = root.resolve()

    def _read(rel: str) -> str | None:
        if not rel.endswith(".rs"):
            return None
        candidate = (resolved_root / rel).resolve()
        if not candidate.is_relative_to(resolved_root):
            return None
        try:
            return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    return _read


def reconstruction_file_reader(
    cluster_root: Path, project_root: Path,
) -> Callable[[str], str | None]:
    """FILE-kind reader for the task2a fill routes, which see the crate across
    TWO trees that a single-root reader cannot both serve.

    During task2a the cluster's reconstruction lives under `cluster_root`
    (`.skeleton-filled/<cluster>`, whose `src/` holds the bodies being filled),
    while the oracle tests live under `project_root` (`tmp/<project>/tests`,
    which the crate's own `tests/*.rs` call helpers from) — and
    `project_root/src` is only the post-strip shell (a one-line `lib.rs` plus
    the `*.gen_tests_*.rs`). A reader locked to `cluster_root` alone refuses a
    `tests/common.rs` request, because the cluster tree's `tests` is a symlink
    to `project_root/tests` and `_read` resolves symlinks before the
    containment check, so the target lands outside `cluster_root` and is
    dropped (measured: `4911-9ab8852e` iter 1, the `src/error.rs` file-first
    reply asked `tests/common.rs` — where `bail_literal()`/`bail_fmt()` that
    the quoted `tests/test_downcast.rs` calls are defined — and got
    `no such file in this crate`). A reader locked to `project_root` alone
    instead serves the shell for every `src/*.rs` request, blinding the route
    to the reconstruction's own sibling bodies.

    So try `cluster_root` first (the reconstruction's `src/`), then fall
    through to `project_root` (the oracle's `tests/`). Containment holds on
    EACH root, and no upstream body is reachable from either: `cluster_root`
    is the reconstruction the loop authored, `project_root/src` is the stripped
    shell, `project_root/tests` IS the oracle, and no `.rs` under
    `.original_structure`/`.cluster_augmented` exists (only API `.md`/`.json`).
    """
    _cluster = workspace_file_reader(cluster_root)
    _project = workspace_file_reader(project_root)

    def _read(rel: str) -> str | None:
        # Fall through on ABSENCE only (`None`), never on emptiness: an empty
        # cluster file is the reconstruction's answer for that path, and the
        # project root must not be consulted for it — its `src/` is the shell.
        text = _cluster(rel)
        return text if text is not None else _project(rel)

    return _read


def _windowed(pull: ContextPull, text: str) -> PullAnswer:
    """The request's line window of `text` under its kind's cap. Every kind
    is windowed the same way, so no capped answer is ever a head the model
    cannot see past."""
    win = window_lines(text, pull.from_line, _CAPS[pull.kind])
    if win is None:
        total = len(text.splitlines())
        return PullAnswer(
            pull, f"from_line {pull.from_line} is past the end "
                  f"({total} line(s))", ok=False)
    return PullAnswer(pull, win.text, truncated=win.truncated,
                      first_line=win.first_line, last_line=win.last_line,
                      total_lines=win.total_lines)


def resolve_pull(pull: ContextPull, sources: PullSources) -> PullAnswer:
    """Answer one request from the injected sources. Never raises — a failed
    read becomes an `ok=False` answer the model can act on."""
    try:
        if pull.kind is PullKind.FILE:
            text = sources.read_file(pull.arg)
            if text is None:
                return PullAnswer(pull, f"no such file in this crate: {pull.arg}",
                                  ok=False)
            return _windowed(pull, text)
        if pull.kind is PullKind.TEST_SOURCE:
            text = sources.find_tests(pull.arg)
            if not text.strip():
                return PullAnswer(pull, f"no test named {pull.arg} found", ok=False)
            return _windowed(pull, text)
        if pull.kind is PullKind.TYPE_DEF:
            text = sources.type_def(pull.arg)
            if not text.strip():
                return PullAnswer(pull, f"no type named {pull.arg} in scope",
                                  ok=False)
            return _windowed(pull, text)
        if pull.kind is PullKind.FULL_ERROR:
            text = sources.full_error()
            if not text:
                return PullAnswer(pull, "(no compiler output captured)", ok=False)
            return _windowed(pull, text)
        if pull.kind is PullKind.MACRO_EXPANSION and sources.expand_macro:
            return _windowed(pull, sources.expand_macro(pull.arg))
        return PullAnswer(pull, f"kind not available: {pull.kind}", ok=False)
    except Exception as e:  # a pull must never kill a fill
        return PullAnswer(pull, f"lookup failed: {e}", ok=False)


#: What one task has already been sent, by request (window included): the
#: payload bytes. Every caller keeps every answer in the conversation it
#: re-asks with (the accumulated prompt, the ladder's augmented prompt, the
#: session transcript), so a byte-identical answer is already above.
ServedAnswers = dict[ContextPull, str]


def _held_whole(pull: ContextPull, sources: PullSources,
                conversation: str) -> PullAnswer | None:
    """The answer to a whole-file request for a file `conversation` already
    carries as a declared whole view: where it is, what was rewritten on disk
    since, and an outline — never the file again. None when the request is
    for a window (`from_line`), the conversation holds no whole view of the
    path, or the file cannot be read (then the ordinary answer applies).

    Measured before this existed (2026-09-01 → 2026-09-24): the per-fn view
    of `src/error.rs` is the file with the cluster's inactive cfg arms
    removed, and the re-sent answer was the raw file — 11 to 14 more code
    lines, all inactive — so the ask bought the same code plus noise."""
    if pull.kind is not PullKind.FILE or pull.from_line > 1 or not conversation:
        return None
    held = read_holdings(conversation).whole_view(pull.arg)
    if held is None:
        return None
    try:
        disk = sources.read_file(pull.arg)
    except Exception:  # a pull must never kill a fill
        return None
    if disk is None:
        return None
    return PullAnswer(pull, held_file_answer(pull.arg, held, disk))


def _note_shown_type(answer: PullAnswer, conversation: str) -> PullAnswer:
    """A type_def answer for a type the conversation's TYPES_IN_SCOPE already
    defines says so first. The definition is still sent — it is short, and
    the model asked because it did not find it — but the next ask for a
    listed type is answered by the index the prompt opens with. Half of the
    type_def requests from 2026-09-01 to 2026-09-24 (81 of 163) were for a
    type the prompt had rendered."""
    if (answer.pull.kind is not PullKind.TYPE_DEF or not answer.ok
            or not conversation
            or answer.pull.arg not in read_holdings(conversation).types):
        return answer
    return replace(answer, payload=(
        f"({answer.pull.arg} is already defined in TYPES_IN_SCOPE above; "
        "listed in the contents index at the top. Its definition again:)\n"
        + answer.payload))


def _answer_affordable(
    pulls: Sequence[ContextPull],
    budget: PullBudget,
    sources: PullSources,
    served: ServedAnswers,
    *,
    log: Callable[[str], None],
    emit: Callable[..., None],
    conversation: str = "",
) -> tuple[list[tuple[PullAnswer, bool]], PullBudget, tuple[ContextPull, ...]]:
    """Answer `pulls` in document order for as long as `budget` lasts.

    Returns the answers each paired with whether it REPEATS a byte-identical
    answer already served in this task, the reduced budget, and the requests
    the budget could not reach. Every ANSWERED request costs one unit, repeats
    included, so the cap stays a cap on round-trips: batching several into one
    reply buys the model fewer round-trips, never more context, and a model
    re-asking the same thing cannot loop.

    `conversation` is everything the model has been sent in this task; a
    whole-file request for a file it holds whole is answered by `_held_whole`."""
    answers: list[tuple[PullAnswer, bool]] = []
    for i, pull in enumerate(pulls):
        spent = budget.spend()
        if spent is None:
            return answers, budget, tuple(pulls[i:])
        budget = spent
        held = _held_whole(pull, sources, conversation)
        answer = held if held is not None else resolve_pull(pull, sources)
        answer = _note_shown_type(answer, conversation)
        repeat = answer.ok and served.get(pull) == answer.payload
        if answer.ok:
            served[pull] = answer.payload
        window = (f", lines {answer.first_line}-{answer.last_line} of "
                  f"{answer.total_lines}" if answer.total_lines else "")
        log(f"  [pull] {pull.tag()} → "
            f"{'ok' if answer.ok else 'unavailable'} "
            f"({len(answer.payload)} chars{window}"
            f"{', truncated' if answer.truncated else ''}"
            f"{', already held whole — pointed at it' if held is not None else ''}"
            f"{', repeat — not re-sent' if repeat else ''})")
        emit("pull.answer", data={
            "kind": str(pull.kind), "arg": pull.arg, "ok": answer.ok,
            "chars": len(answer.payload), "truncated": answer.truncated,
            "from_line": pull.from_line, "last_line": answer.last_line,
            "total_lines": answer.total_lines, "repeat": repeat,
            "held_whole": held is not None,
            "remaining": budget.remaining})
        answers.append((answer, repeat))
    return answers, budget, ()


def _log_refusals(pulls: Sequence[ContextPull],
                  log: Callable[[str], None]) -> None:
    for pull in pulls:
        log(f"  [pull] budget spent — refusing {pull.tag()}; "
            "asking once more for a body")


class RequestAnswerer(Protocol):
    """Given a raw reply and what the conversation has sent so far, the
    follow-up addition for its requests, or None."""

    def __call__(self, response: str, conversation: str = "") -> str | None: ...


class ConversationRequests:
    """The requests of one conversation, answered under a round-trip budget.

    Called with a raw reply, returns the follow-up turn for its requests —
    or None when there is no well-formed request, or the budget is spent AND
    the model has already been told so. The one refusal notice is what stops
    a conversation from spending its calls on a model that is asking rather
    than answering: without it the next call re-asks the same question,
    which says nothing about the spent budget, so the model asks again.

    `open_turn` restores the budget for the next turn the caller opens (a
    fill's retry is a new question and may ask again); answers already
    served stay served for the conversation's lifetime, so a byte-identical
    answer is flagged as a repeat instead of re-sent unnoticed. A session
    never calls it: its budget is the session's."""

    def __init__(self, sources: PullSources, *, log: Callable[[str], None],
                 emit: Callable[..., None], max_pulls: int = 2) -> None:
        self._sources = sources
        self._log = log
        self._emit = emit
        self._max = max_pulls
        self._budget = PullBudget(max_pulls)
        self._warned = False
        self._served: ServedAnswers = {}

    @property
    def sources(self) -> PullSources:
        return self._sources

    def open_turn(self) -> None:
        self._budget = PullBudget(self._max)
        self._warned = False

    def __call__(self, response: str, conversation: str = "") -> str | None:
        pulls = extract_context_pulls(response)
        if not pulls:
            return None
        answers, self._budget, unanswered = _answer_affordable(
            pulls, self._budget, self._sources, self._served,
            log=self._log, emit=self._emit, conversation=conversation)
        if answers:
            return render_pull_followups(answers)
        if self._warned:
            return None
        self._warned = True
        _log_refusals(unanswered, self._log)
        return render_pull_budget_spent(unanswered)


def make_request_answerer(
    sources: PullSources,
    *,
    log: Callable[[str], None],
    emit: Callable[..., None],
    max_pulls: int = 2,
) -> ConversationRequests:
    """The requests of one conversation (`ConversationRequests`)."""
    return ConversationRequests(sources, log=log, emit=emit, max_pulls=max_pulls)
