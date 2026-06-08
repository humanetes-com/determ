"""The Python↔transport seam: normalized stream events + the Transport Protocol.

A transport's only job is to turn one request into a stream of :class:`Delta`
(``content`` / ``reasoning`` split already done, whatever the wire format), fill a
:class:`StreamMeta`, and raise the typed errors below so the controller can apply a
*single* retry/recovery policy regardless of backend.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .budget import BudgetSpec, BudgetState


@dataclass(slots=True)
class Delta:
    """One streamed increment, already demuxed into response vs reasoning text."""

    content: str = ""
    reasoning: str = ""


@dataclass(slots=True)
class StreamMeta:
    """Out-param the transport fills while streaming (read after the loop)."""

    finish_reason: str | None = None     # "length" ⇒ truncated at max_tokens; "stop" ⇒ EOS
    prompt_tokens: int | None = None     # actual input tokens, when the server reports them


class InputTooLong(Exception):
    """Transport rejected the prompt as too long; carries the true token count if known."""

    def __init__(self, real_tokens: int | None = None) -> None:
        super().__init__(f"input too long (real_tokens={real_tokens})")
        self.real_tokens = real_tokens


class TransportUnavailable(Exception):
    """Transient transport/network failure; controller may retry after wait_until_ready()."""


class TransportTimeout(Exception):
    """No token arrived within the silence window; controller returns partial/empty, no retry."""


@runtime_checkable
class Transport(Protocol):
    """A dumb token pipe. No recovery, no capping, no followup policy."""

    served_model: str
    supports_followup: bool   # can re-feed reasoning as an assistant turn to recover code

    def spec(self) -> BudgetSpec:
        """This transport's budget shape (ctx, content/thinking budgets, cap mode)."""
        ...

    def budget_state(self) -> BudgetState:
        """The persistent learned chars-per-token state (mutated across calls)."""
        ...

    def resolve_thinking(self, model: str, want_think: bool | None) -> bool:
        """Effective thinking mode (caller intent + this transport's config)."""
        ...

    def stream(
        self,
        messages: list[dict],
        *,
        model: str,
        max_tokens: int | None,
        enable_thinking: bool,
        meta: StreamMeta,
    ) -> Iterator[Delta]:
        """Yield normalized deltas. Raise InputTooLong / TransportUnavailable / TransportTimeout."""
        ...

    def followup_messages(self, base_messages: list[dict], reasoning_text: str,
                          *, model: str) -> list[dict]:
        """Build the messages for the code-only recovery turn (assistant-turn format varies)."""
        ...

    def clean(self, text: str, *, model: str) -> str:
        """Strip this wire format's boundary/role tokens from a finished response."""
        ...

    def wait_until_ready(self) -> None:
        """Block until the server accepts requests again (after TransportUnavailable)."""
        ...
