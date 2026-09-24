"""One conversation per fill unit — the only way a fill route talks to the model.

Before this, a fill route (file-first, per-impl, per-fn, pair, and task3's
fix unit) made one call per attempt and answered "the prompt lacks
something" three unrelated ways: `ask_with_pulls` appended the answer to the
prompt and DROPPED the model's own request turn; the fix loop rebuilt the
whole prompt for the next attempt and summarised the model's earlier replies
back to it (`<PRIOR_ATTEMPTS>`, `<LAST_ERROR>`, `prior_error`); only the
slice session kept a transcript. Measured over 2026-09-01 → 2026-09-24
(`scripts/prompt_sufficiency_census.py`): 690 mid-call requests, 302 of them
for something the prompt already rendered, each answered by re-sending the
whole prompt plus the answer to a model that no longer had its reasoning;
and the session — the one route that kept the conversation and judged turns
inside the fill — paid 38.7 tests per 100 calls against per-fn 29.7, file
12.6, pair 4.6 and impl 0.

`UnitConversation` is that loop once. A route opens it with its turn-0
prompt, and every later call is a turn on the same transcript: the answer to
a request (the request stays above it), a retry carrying what the route's
gates measured (`prompts.retry_turn.AttemptFeedback`), or a re-ask after a
reply that carried no body. A route cannot forget the model's reasoning
between calls because it has no other way to call the model.

What it deliberately does not own: acceptance. Each route keeps its gates
(splice, compile, U2, the regression ratchet, the peel, `judge_against_best`)
and tells the conversation what they measured; none is re-implemented here.

Bounds. `max_transcript_chars` is checked BEFORE a call — a turn that would
carry the transcript past it is not sent (`TranscriptBound`), the same rule
the session applies (run `366282-7c585f74`, 2026-09-15: one 135,396-char
reply took a session transcript past the single-call limit and the next
turn was sent anyway). The opening turn is fitted to the context once, when
it is opened, so the transcript's prefix never changes under the model. The
unit's wall-clock bound, the fill share and graceful stop stay with the
route's fix-loop driver (`fix_loop.drive_fix_loop` reads `unit_clock`), which
decides whether another turn is opened at all.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipeline_core.application.reconstruct.pull_resolver import (
    ConversationRequests,
)
from pipeline_core.domain.prompt_holdings import (
    changed_since,
    disk_items_for,
    read_holdings,
)
from pipeline_core.prompts.retry_turn import (
    AttemptFeedback,
    render_no_body_turn,
    render_retry_turn,
)


class TranscriptBound(Exception):
    """The next turn would carry the transcript past its bound; it was not
    sent. The route ends the unit's attempts there, as the session does."""


def _identity(text: str) -> str:
    return text


def _no_emit(*_a: Any, **_k: Any) -> None:
    return None


@dataclass(frozen=True, slots=True)
class ConversationPorts:
    """The transport and its bounds.

    `ask_turns(messages, **call_kwargs)` is the transcript call
    (`AskClient.ask_turns` bound to the route's model). `fit(text)` fits the
    opening turn to the context the way a single-prompt call is fitted
    (`AskClient.fit`); later turns are never trimmed — a conversation with a
    hole in it is not a cheaper conversation."""

    ask_turns: Callable[..., str]
    log: Callable[[str], None]
    emit: Callable[..., None] = _no_emit
    fit: Callable[[str], str] = _identity
    max_transcript_chars: int = 0


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _fn_name_of(header: str) -> str:
    """`pub(crate) fn name<T>(…)` -> `name`."""
    rest = header.split("fn ", 1)[-1]
    out = ""
    for ch in rest:
        if ch.isalnum() or ch == "_":
            out += ch
        else:
            break
    return out


class UnitConversation:
    """The transcript of one fill unit and the turns it may send."""

    def __init__(self, ports: ConversationPorts, *, label: str, route: str,
                 requests: ConversationRequests | None = None,
                 menu: str = "") -> None:
        self._ports = ports
        self.label = label
        self.route = route
        self._requests = requests
        self._menu = menu
        self.messages: list[dict[str, Any]] = []
        self.calls = 0
        # (header, disk body text) pairs already reported as moved, so a
        # later turn does not repeat them.
        self._reported: set[tuple[str, str]] = set()

    # ── state ────────────────────────────────────────────────────────────
    @property
    def opened(self) -> bool:
        return bool(self.messages)

    def text(self) -> str:
        """Everything the model has been sent and has said, in order."""
        return "\n\n".join(str(m.get("content") or "") for m in self.messages)

    def chars(self) -> int:
        return sum(len(str(m.get("content") or "")) for m in self.messages)

    # ── calls ────────────────────────────────────────────────────────────
    def _exchange(self, text: str, kind: str, **call_kwargs: Any) -> str:
        bound = self._ports.max_transcript_chars
        if 0 < bound < self.chars() + len(text):
            self._ports.log(
                f"  [conversation/{self.route}] {self.label}: the next turn "
                f"would carry the transcript to {self.chars() + len(text):,} "
                f"chars, over the single-call bound ({bound:,}), after "
                f"{self.calls} call(s) — not sent.")
            self._ports.emit("unit.conversation.bound", data={
                "route": self.route, "unit": self.label, "calls": self.calls,
                "chars": self.chars() + len(text), "bound": bound})
            raise TranscriptBound(self.label)
        self.messages.append(_user(text))
        try:
            reply = str(self._ports.ask_turns(list(self.messages), **call_kwargs))
        except BaseException:
            self.messages.pop()
            raise
        self.calls += 1
        self.messages.append(_assistant(reply))
        self._ports.emit("unit.conversation.turn", data={
            "route": self.route, "unit": self.label, "call": self.calls,
            "kind": kind, "sent_chars": len(text), "reply_chars": len(reply),
            "transcript_chars": self.chars()})
        return reply

    def _answered(self, reply: str, has_deliverable: Callable[[str], bool],
                  **call_kwargs: Any) -> str:
        """Answer the reply's requests on the transcript until it delivers,
        stops asking, or the turn's request budget is spent."""
        while (self._requests is not None and not has_deliverable(reply)):
            follow = self._requests(reply, conversation=self.text())
            if follow is None:
                return reply
            reply = self._exchange(follow, "request", **call_kwargs)
        return reply

    def open(self, prompt: str, *, has_deliverable: Callable[[str], bool],
             **call_kwargs: Any) -> str:
        """Turn 0: the route's built prompt, fitted to the context once, with
        the request menu when requests are served."""
        if self.opened:
            raise RuntimeError(f"{self.label}: conversation already opened")
        if self._requests is not None:
            self._requests.open_turn()
        first = self._ports.fit(prompt + self._menu)
        reply = self._exchange(first, "open", **call_kwargs)
        return self._answered(reply, has_deliverable, **call_kwargs)

    def retry(self, feedback: AttemptFeedback, *,
              has_deliverable: Callable[[str], bool],
              **call_kwargs: Any) -> str:
        """A turn carrying what the route's gates measured about the last
        reply it did not keep."""
        if not self.opened:
            raise RuntimeError(f"{self.label}: retry before open")
        if self._requests is not None:
            self._requests.open_turn()
        turn = render_retry_turn(feedback, conversation=self.text())
        reply = self._exchange(turn, "retry", **call_kwargs)
        return self._answered(reply, has_deliverable, **call_kwargs)

    def reask(self, output: str, targets: tuple[str, ...], *,
              has_deliverable: Callable[[str], bool],
              **call_kwargs: Any) -> str:
        """The reply carried no body and no request: ask for the answer,
        on the same transcript (its reasoning is above)."""
        reply = self._exchange(render_no_body_turn(output, targets), "reask",
                               **call_kwargs)
        return self._answered(reply, has_deliverable, **call_kwargs)

    def say(self, text: str, *, kind: str = "say", **call_kwargs: Any) -> str:
        """One plain turn with no request handling — the slice session's
        call, whose requests are turns it counts, admits and budgets itself."""
        if not self.opened:
            text = self._ports.fit(text)
        return self._exchange(text, kind, **call_kwargs)

    # ── what changed on disk since the conversation's views ─────────────
    def bodies_moved(self, read_file: Callable[[str], str | None],
                     exclude: frozenset[str] = frozenset(), *,
                     normalize: Callable[[str], str] = _identity) -> str:
        """The current text of every body another fill rewrote on disk since
        the conversation's latest view of its file was rendered, excluding
        fns named in `exclude` (the unit's own targets: their state is the
        route's to report). Each moved body is reported once.

        `normalize` is applied to the view AND the disk before they are
        compared — the route's cluster cfg filter, so a view rendered with
        the inactive arms removed is not read as a rewrite of the raw file.
        It must be idempotent; the filter is."""
        latest: dict[str, str] = {}
        for held in read_holdings(self.text()).files:
            latest[held.path] = held.text
        parts: list[str] = []
        for path, view in latest.items():
            try:
                raw = read_file(path)
            except Exception:  # noqa: BLE001 — a reading never ends a unit
                raw = None
            if not raw:
                continue
            disk = normalize(raw)
            heads = [h for h in changed_since(normalize(view), disk)
                     if _fn_name_of(h) not in exclude]
            fresh: list[str] = []
            for h in heads:
                key = (h, " ".join(disk_items_for(disk, (h,)).split()))
                if key in self._reported:
                    continue
                self._reported.add(key)
                fresh.append(h)
            if fresh:
                parts.append(f"// {path}\n" + disk_items_for(disk, fresh))
        return "\n\n".join(parts)


__all__ = ["ConversationPorts", "TranscriptBound", "UnitConversation"]
