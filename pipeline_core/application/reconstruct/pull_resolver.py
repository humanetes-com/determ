"""Pull-protocol resolver + bounded ask loop (july_plan P2) — application.

`PullSources` is the ISP seam: five injected reads, all post-strip-legal
(Hard Rule 11). `workspace_file_reader` builds the FILE read with structural
path containment — any path resolving outside the reconstruction tree is
refused, so original source is unreachable by construction, not by policy.

`ask_with_pulls` is the ONE round-trip driver every fill path shares:
deliverable-first (a reply that already carries FN bodies is never delayed by
its stray request tag), then at most `PullBudget.remaining` answered requests,
then the final reply as-is.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.context_pull import (
    ContextPull,
    PullAnswer,
    PullBudget,
    PullKind,
    extract_context_pull,
)
from pipeline_core.prompts.pull_menu import render_pull_followup

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
    """FILE-kind reader locked to the reconstruction tree (HR 11): the
    resolved path must stay under `root` and be a `.rs` file."""
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


def make_request_answerer(
    sources: PullSources,
    *,
    log: Callable[[str], None],
    emit: Callable[..., None],
    max_pulls: int = 2,
) -> Callable[[str], str | None]:
    """A stateful answerer for ask ladders that own their retry policy (the
    per-fn 3-rung ladder): given a raw response, returns the follow-up prompt
    ADDITION for its request — or None when there is no well-formed request
    or the shared budget is spent. Budget persists across the ladder's rungs."""
    budget = PullBudget(max_pulls)

    def _answer(response: str) -> str | None:
        nonlocal budget
        pull = extract_context_pull(response)
        if pull is None:
            return None
        spent = budget.spend()
        if spent is None:
            log(f"  [pull] budget exhausted — ignoring {pull.tag()}")
            return None
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
        return render_pull_followup(pull, answer)

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
    Deliverable replies short-circuit; unanswerable/malformed requests end
    the loop with the reply as-is (the caller's normal no-bodies handling
    applies)."""
    budget = PullBudget(max_pulls)
    current = prompt
    while True:
        response = ask(current)
        if has_deliverable(response):
            return response
        pull = extract_context_pull(response)
        if pull is None:
            return response
        spent = budget.spend()
        if spent is None:
            log(f"  [pull] budget exhausted — ignoring {pull.tag()}")
            return response
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
        current = current + render_pull_followup(pull, answer)
