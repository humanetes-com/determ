"""Wire-format response cleanup (pure).

Strips chat-boundary / role-echo / leaked-think tokens from a finished response.
Returns the cleaned text plus a list of (guard_location, message) events so the
caller can log them through its own guard sink. Verbatim transcription of the
per-backend strip blocks it centralizes.
"""

from __future__ import annotations

import re

GuardEvent = tuple[str, str]

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_ROLE_ECHO = re.compile(r"<\|endoftext\|>\s*<\|im_start\|>(user|system|assistant)\b.*", re.DOTALL)
_NUMBERED = re.compile(r"<\|endoftext\|>\s*<\|im_start\|>\s*\d+\..*", re.DOTALL)
_EOS = re.compile(r"<\|endoftext\|>")
_IM_START = re.compile(r"<\|im_start\|>[^\n]*\n?")
_IM_END = re.compile(r"<\|im_end\|>")


def strip_think(text: str) -> str:
    """Defensively remove any complete <think>...</think> pair that leaked into content."""
    return _THINK.sub("", text).strip()


def strip_chat_boundaries(text: str, *, model: str) -> tuple[str, list[GuardEvent]]:
    """Full chat-template boundary cleanup (Qwen <|...|> tokens), with guard events."""
    events: list[GuardEvent] = []
    result = _THINK.sub("", text)

    # Truncate at EOS followed by a role turn — the model echoing the prompt back.
    _before = result
    result = _ROLE_ECHO.sub("", result)
    if result != _before:
        events.append((
            "compat/endoftext_role_echo",
            f"model={model} truncated at <|endoftext|><|im_start|>role — "
            f"dropped {len(_before) - len(result):,} chars of echoed prompt",
        ))

    # Truncate at EOS followed by numbered alternatives (1., 2., …); else strip bare tokens.
    _before = result
    result = _NUMBERED.sub("", result)
    if result != _before:
        events.append((
            "compat/endoftext_stripped",
            f"model={model} truncated at first EOS (numbered alternatives detected — "
            f"dropped {len(_before) - len(result):,} chars)",
        ))
    else:
        _eos_count = result.count("<|endoftext|>")
        _im_count = result.count("<|im_start|>")
        result = _EOS.sub("", result)
        result = _IM_START.sub("", result)
        result = _IM_END.sub("", result)
        if _eos_count or _im_count:
            events.append((
                "compat/endoftext_stripped",
                f"model={model} stripped {_eos_count} EOS + {_im_count} im_start tokens "
                f"(bare boundaries)",
            ))
    return result.strip(), events
