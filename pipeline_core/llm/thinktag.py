"""Incremental ``<think>...</think>`` demuxer for transports that inline reasoning.

Some servers (the Ollama OpenAI-compat layer) mux reasoning into the content stream
as ``<think>...</think>`` instead of a separate field. Feeding that raw to the
structural detector would brace-count code *inside* the reasoning and to the
thinking-cap would never see reasoning as reasoning. This splitter routes each
streamed piece into (content, reasoning) so the controller treats every transport
uniformly. Tags may straddle chunk boundaries, so a partial-tag tail is held back.
"""

from __future__ import annotations

_OPEN = "<think>"
_CLOSE = "</think>"


def _max_partial(s: str, tag: str) -> int:
    """Length of the longest proper prefix of `tag` that is a suffix of `s` (tail hold-back)."""
    for k in range(min(len(s), len(tag) - 1), 0, -1):
        if s.endswith(tag[:k]):
            return k
    return 0


class InlineThinkSplitter:
    __slots__ = ("_buf", "_in_think")

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> tuple[str, str]:
        """Consume one streamed piece; return (content, reasoning) extracted so far."""
        self._buf += text
        content: list[str] = []
        reasoning: list[str] = []
        while True:
            if not self._in_think:
                idx = self._buf.find(_OPEN)
                if idx == -1:
                    hold = _max_partial(self._buf, _OPEN)
                    cut = len(self._buf) - hold
                    content.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                content.append(self._buf[:idx])
                self._buf = self._buf[idx + len(_OPEN):]
                self._in_think = True
            else:
                idx = self._buf.find(_CLOSE)
                if idx == -1:
                    hold = _max_partial(self._buf, _CLOSE)
                    cut = len(self._buf) - hold
                    reasoning.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                reasoning.append(self._buf[:idx])
                self._buf = self._buf[idx + len(_CLOSE):]
                self._in_think = False
        return "".join(content), "".join(reasoning)

    def finish(self) -> tuple[str, str]:
        """Flush the held-back tail at stream end. Unclosed <think> ⇒ the remainder is reasoning."""
        rem = self._buf
        self._buf = ""
        return ("", rem) if self._in_think else (rem, "")
