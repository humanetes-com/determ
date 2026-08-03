"""Pull-protocol resolver + bounded ask loop — application.

`PullSources` is the ISP seam: five injected reads, every one of them served
from what the reconstruction is allowed to see (workspace tree, tests, baked
type records, cargo output). `workspace_file_reader` builds the FILE read with
structural path containment — any path resolving outside the reconstruction
tree is refused, so the upstream crate source is unreachable by construction
rather than by policy.

`ask_with_pulls` is the ONE round-trip driver every fill path shares:
deliverable-first (a reply that already carries FN bodies is never delayed by
its stray request tag), then at most `PullBudget.remaining` answered requests —
ALL of a reply's requests are served, not just its last — then one final ask
that tells a still-asking model the budget is spent, then the reply as-is.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.context_pull import (
    ContextPull,
    PullAnswer,
    PullBudget,
    PullKind,
    extract_context_pulls,
)
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


def _cap(kind: PullKind, payload: str) -> tuple[str, bool]:
    cap = _CAPS[kind]
    if len(payload) > cap:
        return payload[:cap], True
    return payload, False


def resolve_pull(pull: ContextPull, sources: PullSources) -> PullAnswer:
    """Answer one request from the injected sources. Never raises — a failed
    read becomes an `ok=False` answer the model can act on."""
    try:
        if pull.kind is PullKind.FILE:
            text = sources.read_file(pull.arg)
            if text is None:
                return PullAnswer(pull, f"no such file in this crate: {pull.arg}",
                                  ok=False)
            payload, trunc = _cap(pull.kind, text)
            return PullAnswer(pull, payload, truncated=trunc)
        if pull.kind is PullKind.TEST_SOURCE:
            text = sources.find_tests(pull.arg)
            if not text.strip():
                return PullAnswer(pull, f"no test named {pull.arg} found", ok=False)
            payload, trunc = _cap(pull.kind, text)
            return PullAnswer(pull, payload, truncated=trunc)
        if pull.kind is PullKind.TYPE_DEF:
            text = sources.type_def(pull.arg)
            if not text.strip():
                return PullAnswer(pull, f"no type named {pull.arg} in scope",
                                  ok=False)
            payload, trunc = _cap(pull.kind, text)
            return PullAnswer(pull, payload, truncated=trunc)
        if pull.kind is PullKind.FULL_ERROR:
            payload, trunc = _cap(pull.kind, sources.full_error())
            return PullAnswer(pull, payload or "(no compiler output captured)",
                              ok=bool(payload), truncated=trunc)
        if pull.kind is PullKind.MACRO_EXPANSION and sources.expand_macro:
            payload, trunc = _cap(pull.kind, sources.expand_macro(pull.arg))
            return PullAnswer(pull, payload, truncated=trunc)
        return PullAnswer(pull, f"kind not available: {pull.kind}", ok=False)
    except Exception as e:  # a pull must never kill a fill
        return PullAnswer(pull, f"lookup failed: {e}", ok=False)


def _answer_affordable(
    pulls: Sequence[ContextPull],
    budget: PullBudget,
    sources: PullSources,
    *,
    log: Callable[[str], None],
    emit: Callable[..., None],
) -> tuple[list[PullAnswer], PullBudget, tuple[ContextPull, ...]]:
    """Answer `pulls` in document order for as long as `budget` lasts.

    Returns the answers, the reduced budget, and the requests the budget could
    not reach. Every ANSWERED request costs one unit, so the cap stays a cap on
    artifacts served: batching several into one reply buys the model fewer
    round-trips, never more context."""
    answers: list[PullAnswer] = []
    for i, pull in enumerate(pulls):
        spent = budget.spend()
        if spent is None:
            return answers, budget, tuple(pulls[i:])
        budget = spent
        answer = resolve_pull(pull, sources)
        log(f"  [pull] {pull.tag()} → "
            f"{'ok' if answer.ok else 'unavailable'} "
            f"({len(answer.payload)} chars"
            f"{', truncated' if answer.truncated else ''})")
        emit("pull.answer", data={
            "kind": str(pull.kind), "arg": pull.arg, "ok": answer.ok,
            "chars": len(answer.payload), "truncated": answer.truncated,
            "remaining": budget.remaining})
        answers.append(answer)
    return answers, budget, ()


def _log_refusals(pulls: Sequence[ContextPull],
                  log: Callable[[str], None]) -> None:
    for pull in pulls:
        log(f"  [pull] budget spent — refusing {pull.tag()}; "
            "asking once more for a body")


def make_request_answerer(
    sources: PullSources,
    *,
    log: Callable[[str], None],
    emit: Callable[..., None],
    max_pulls: int = 2,
) -> Callable[[str], str | None]:
    """A stateful answerer for ask ladders that own their retry policy (the
    per-fn 3-rung ladder): given a raw response, returns the follow-up prompt
    ADDITION for its requests — or None when there is no well-formed request,
    or the budget is spent AND the model has already been told so. Budget
    persists across the ladder's rungs.

    The one refusal notice is what stops the ladder burning its remaining rungs
    on a model that is asking rather than answering: without it the rung breaks
    and the next rung re-asks the ORIGINAL prompt, which says nothing about the
    spent budget, so the model asks again and rungs 2 and 3 go the same way."""
    budget = PullBudget(max_pulls)
    warned = False

    def _answer(response: str) -> str | None:
        nonlocal budget, warned
        pulls = extract_context_pulls(response)
        if not pulls:
            return None
        answers, budget, unanswered = _answer_affordable(
            pulls, budget, sources, log=log, emit=emit)
        if answers:
            return render_pull_followups(answers)
        if warned:
            return None
        warned = True
        _log_refusals(unanswered, log)
        return render_pull_budget_spent(unanswered)

    return _answer


def ask_with_pulls(
    ask: Callable[[str], str],
    prompt: str,
    *,
    sources: PullSources,
    has_deliverable: Callable[[str], bool],
    log: Callable[[str], None],
    emit: Callable[..., None],
    max_pulls: int = 2,
) -> str:
    """Drive one fill exchange with at most `max_pulls` answered requests.
    Deliverable replies short-circuit; a reply with no well-formed request
    ends the loop as-is (the caller's normal no-bodies handling applies).

    A reply that STILL asks once the budget is spent gets one final ask that
    says so, and that reply is returned whatever it holds. Returning the
    request-only reply instead is a whole discarded attempt: on 2026-08-03 the
    nightly src/error.rs file-first fill spent three round-trips and nine
    minutes on a 40 000-char prompt, had the crash diagnosed in prose, asked
    for one more type, and was logged `empty/no FN blocks (0 FN tag(s) in
    reply) — falling back to per-block fills`. The model had not failed; it was
    waiting on an answer nothing was going to send. Total asks stay bounded at
    `max_pulls + 2` and the notice is issued at most once."""
    budget = PullBudget(max_pulls)
    current = prompt
    warned = False
    while True:
        response = ask(current)
        if has_deliverable(response):
            return response
        pulls = extract_context_pulls(response)
        if not pulls:
            return response
        answers, budget, unanswered = _answer_affordable(
            pulls, budget, sources, log=log, emit=emit)
        if answers:
            current = current + render_pull_followups(answers)
            continue
        if warned:
            return response
        warned = True
        _log_refusals(unanswered, log)
        current = current + render_pull_budget_spent(unanswered)
